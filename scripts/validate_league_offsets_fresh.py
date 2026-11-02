"""Freeze offset+refit, score new untouched history, and archive future forecasts.

Offline only: never activates models, writes Supabase, or changes report caches.
Run again with --score-prospective to score the immutable pre-kickoff archive.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil

import numpy as np
from sqlalchemy import text
from match_pool_module import forecast as f, forecast_candidates as c
from match_pool_module.forecast_history import daily_boundary
from match_pool_module.forecast_league_offsets import AssociationPrior, apply_elo_gap
from scripts.evaluate_league_offsets import (ALIASES, EUROPE, causal_gaps, fit_fixed, load_prior,
                                             membership, main_metrics, paired_bootstrap, rates)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def active_record():
    with f.engine.connect() as db:
        row = db.execute(text('SELECT id,artifact FROM mobile_match_forecast_models WHERE is_active')).mappings().one()
    return row['id'], row['artifact']


def load_context(output):
    model = json.loads((output / 'baseline.json').read_text())
    source = json.loads((output / 'training-data.json').read_text())
    raw_prior = json.loads((output / 'association-prior-2025.json').read_text())
    source['competitions'] = model['competitions']
    teams, _, _ = membership(source, raw_prior)
    countries = {ALIASES.get(r['country'], r['country']) for r in model['competitions'] if r['country'] != 'Europe'}
    countries &= set(raw_prior['coefficients'])
    prior = AssociationPrior({k: raw_prior['coefficients'][k] for k in sorted(countries)}, raw_prior['availableAt'], raw_prior['source'])
    return model, source, teams, prior


def freeze(output, training_path):
    manifest = output / 'freeze.json'
    if manifest.exists():
        frozen = json.loads(manifest.read_text())
        for name, expected in frozen['sha256'].items():
            if digest(output / name) != expected: raise ValueError(f'Frozen artifact changed: {name}')
        return frozen
    model_id, model = active_record()
    source = json.loads(training_path.read_text())
    if source['asOf'] != model['asOf'] or len(source['matches']) != model['inputMatches']:
        raise ValueError('Training snapshot does not match the active production artifact')
    if any(f.utc(m['at']) + timedelta(hours=3) > f.utc(model['asOf']) for m in source['matches']):
        raise ValueError('Training label crosses the freeze training cutoff')
    (output / 'baseline.json').write_text(json.dumps(model, allow_nan=False))
    shutil.copy2(training_path, output / 'training-data.json')
    load_prior(output)
    model, source, teams, prior = load_context(output)
    train = sorted(source['matches'], key=lambda m: (f.utc(m['at']), m['id']))
    gaps, audit = causal_gaps(train, teams, prior)
    data = c.examples(train, model['strategy'])
    selected = {'models': {'incumbent': {'columns': [data['names'].index(key) for key in model['features']],
                                        'regularization': model['regularization']},
                           'nonlinear': model['refinedEnsemble']['nonlinear']}}
    # A control refit must reproduce the actual active model, not an older recipe.
    control, control_tree = fit_fixed(data, model, selected)
    for key in ('beta', 'medians', 'mean', 'scale'):
        np.testing.assert_allclose(control[key], model[key], atol=1e-10, rtol=1e-10)
    assert control_tree['trees'] == model['refinedEnsemble']['nonlinear']['trees']
    shifted = {**data, 'raw': apply_elo_gap(data['raw'], data['names'], [gaps[i] for i in data['ids']])}
    linear, tree = fit_fixed(shifted, model, selected)
    linear['calibration'] = model['goalRateCalibration']
    control['calibration'] = model['goalRateCalibration']
    candidate = {'kind': 'residual_league_offset_refit_v1', 'trainingAsOf': model['asOf'],
                 'strategy': model['strategy'], 'featureNames': data['names'], 'linear': linear, 'nonlinear': tree,
                 'baselineLinear': control, 'weights': model['refinedEnsemble']['weights'],
                 'offsetPriorPenalty': 5., 'offsetDeviationPenalty': 20.}
    (output / 'candidate.json').write_text(json.dumps(candidate, allow_nan=False))
    (output / 'training-offset-audit.json').write_text(json.dumps(audit, indent=2))
    frozen = {'candidateFrozenAt': datetime.now(timezone.utc).isoformat(), 'activeBaselineId': model_id,
              'trainingAsOf': model['asOf'], 'trainingInputMatches': len(train),
              'eligibleTrainingMatches': len(data['ids']), 'baselineControlMatchesProduction': True,
              'promotionAllowed': False, 'candidateSelection': 'offset+refit fixed before collecting new labels',
              'historyPolicy': '04:00 UTC daily updates, minimum three-hour settlement; weights remain frozen',
              'sha256': {name: digest(output / name) for name in ('baseline.json', 'candidate.json', 'training-data.json', 'association-prior-2025.json')}}
    manifest.write_text(json.dumps(frozen, indent=2))
    return frozen


def collect_settled(output, model, frozen):
    path = output / 'fresh-completed.json'
    if path.exists(): return json.loads(path.read_text())
    now = datetime.now(timezone.utc)
    since = frozen['trainingAsOf']
    old_ids = {m['id'] for m in json.loads((output / 'training-data.json').read_text())['matches']}
    def collect(comp):
        return f._history(comp['leagueId'], now - timedelta(hours=3),
                          {'id': comp['seasonId'], 'starting_at': comp['startsAt']}, since=since, include_events=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        batches = list(pool.map(collect, model['competitions']))
    fresh = sorted({m['id']: m for batch in batches for m in batch
                    if m['id'] not in old_ids and f.utc(m['at']) >= f.utc(since)
                    and f.utc(m['at']) + timedelta(hours=3) <= now}.values(), key=lambda m: (f.utc(m['at']), m['id']))
    result = {'collectedAt': now.isoformat(), 'trainingAsOf': since, 'matches': fresh,
              'scope': 'New retrospective holdout: never used in previous reports or any fitting. Not pre-recorded forecasts.'}
    path.write_text(json.dumps(result, allow_nan=False))
    return result


def prediction(state, fixture, baseline, candidate, gap):
    raw = np.array([c.features(state, fixture['home'], fixture['away'], True, fixture['league'], fixture['at'], baseline['strategy']),
                    c.features(state, fixture['away'], fixture['home'], False, fixture['league'], fixture['at'], baseline['strategy'])], float)
    current = rates(candidate['baselineLinear'], baseline['refinedEnsemble']['nonlinear'], raw, candidate['weights'])
    altered = apply_elo_gap(raw, candidate['featureNames'], [gap])
    alternative = rates(candidate['linear'], candidate['nonlinear'], altered, candidate['weights'])
    half_model = {**baseline['halfTimeModel'], 'leagueTotals': {str(k): v for k, v in state.half_totals.items()}}
    models = {}
    for name, values in [('current', current), ('league_offset_refit', alternative)]:
        distribution = f.score_distribution(*values)
        distribution.update(rates=values.tolist(), halfScenarios=f.half_scenarios(*values,
            state.histories[fixture['home']], state.histories[fixture['away']], fixture['league'], half_model))
        models[name] = distribution
    return models


def build_gap_for_future(known, future, teams, prior):
    # Only settled known fixtures contribute labels; future targets are never
    # consumed, and their results are absent from this input.
    from match_pool_module.forecast_league_offsets import LeagueOffsetEstimator
    state = c.History(); observations = []
    for m in known:
        a, b = teams.get(m['home']), teams.get(m['away'])
        if m['league'] in EUROPE and a != b and prior.contrast(a, b) is not None:
            observations.append({'homeAssociation': a, 'awayAssociation': b,
                                 'eloDifference': state.ratings.get(m['home'], 1500.) - state.ratings.get(m['away'], 1500.),
                                 'result': 1. if m['hg'] > m['ag'] else .5 if m['hg'] == m['ag'] else 0.})
        state.consume(m)
    estimator = LeagueOffsetEstimator(prior).fit(observations)
    return state, {m['id']: estimator.gap(teams.get(m['home']), teams.get(m['away'])) for m in future}, estimator.dump()


def fetch_upcoming(model, start, end):
    rows = []
    seasons = {comp['leagueId']: comp['seasonId'] for comp in model['competitions']}
    for page in range(1, 21):
        payload = f._request(f'fixtures/between/{start}/{end}', {
            'include': 'participants;state;league;season', 'filters': 'fixtureLeagues:2,5,2286', 'per_page': 100, 'page': page})
        for fixture in payload.get('data') or []:
            league = int(fixture.get('league_id') or 0)
            if league not in EUROPE or int(fixture.get('season_id') or 0) != seasons.get(league): continue
            sides = {p.get('meta', {}).get('location'): p for p in fixture.get('participants') or []}
            if not all(sides.get(k, {}).get('id') for k in ('home', 'away')): continue
            rows.append({'id': int(fixture['id']), 'at': fixture['starting_at'], 'league': league,
                         'home': int(sides['home']['id']), 'away': int(sides['away']['id']),
                         'homeName': sides['home']['name'], 'awayName': sides['away']['name'],
                         'state': (fixture.get('state') or {}).get('short_name')})
        if not (payload.get('pagination') or {}).get('has_more'): return rows
    raise ValueError('Upcoming fixture pagination exceeded')


def summarize_holdout(output, records, skipped, fresh, frozen, active_after):
    groups = {'all': records, 'domestic': [r for r in records if not r['european']],
              'cross_league': [r for r in records if r['crossLeague']]}
    cohorts = {name: {'models': {model: main_metrics(rows, model) for model in ('current', 'league_offset_refit')},
                     'bootstrap': paired_bootstrap(rows, 'league_offset_refit')}
               for name, rows in groups.items()}
    gate = {}
    if records:
        base, alt = [cohorts['all']['models'][m] for m in ('current', 'league_offset_refit')]
        gate = {'brierNonWorse': alt['brier'] <= base['brier'], 'scoreLogLossNonWorse': alt['scoreLogLoss'] <= base['scoreLogLoss'],
                'exactScoreAccuracyNonLower': alt['exactScoreAccuracy'] >= base['exactScoreAccuracy']}
        gate['passed'] = all(gate.values())
    result = {'scope': fresh['scope'], 'collectedAt': fresh['collectedAt'], 'trainingAsOf': frozen['trainingAsOf'],
              'candidateFrozenAt': frozen['candidateFrozenAt'], 'activeBaselineId': frozen['activeBaselineId'],
              'activeAfter': active_after, 'productionUnchanged': active_after == frozen['activeBaselineId'],
              'newCompletedFixtures': len(fresh['matches']), 'pairedEvaluatedFixtures': len(records), 'skipped': skipped,
              'cohorts': cohorts, 'originalPromotionGate': gate,
              'crossLeagueConclusion': 'pending fresh cross-league fixtures' if not groups['cross_league'] else 'new retrospective holdout; prospective confirmation still pending'}
    (output / 'fresh-results.json').write_text(json.dumps(result, indent=2, allow_nan=False))
    (output / 'fresh-records.json').write_text(json.dumps(records, allow_nan=False))
    return result


def load_archive(output):
    path = output / 'prospective-predictions.json'
    manifest = json.loads((output / 'prospective-manifest.json').read_text())
    if digest(path) != manifest['predictionsSHA256']:
        raise ValueError('Prospective archive checksum changed')
    archive = json.loads(path.read_text())
    rows = archive['predictions']
    if len(rows) != len({row['id'] for row in rows}):
        raise ValueError('Duplicate prospective fixture IDs')
    if any(f.utc(row['generatedAt']) >= f.utc(row['at']) for row in rows):
        raise ValueError('Prediction was not recorded before kickoff')
    return archive


def archive_upcoming(output, model, candidate, source, teams, prior, fresh, frozen):
    # Seal the archive once. Re-running preparation must never replace forecasts
    # or add predictions based on a stale cached collection of match histories.
    if (output / 'prospective-predictions.json').exists():
        return load_archive(output)
    now = datetime.now(timezone.utc)
    future = fetch_upcoming(model, now.date().isoformat(), (now + timedelta(days=7)).date().isoformat())
    future = [m for m in future if f.utc(m['at']) > now and m['state'] in ('NS', 'TBA')]
    known = sorted(source['matches'] + fresh['matches'], key=lambda m: (f.utc(m['at']), m['id']))
    # Prospective snapshots use the last regular daily history boundary, never
    # fixtures occurring after that information cutoff.
    boundary = daily_boundary(now)
    available = [m for m in known if f.utc(m['at']) + timedelta(hours=3) <= boundary]
    state, gaps, estimator = build_gap_for_future(available, future, teams, prior)
    path = output / 'prospective-predictions.json'
    predictions, skipped = [], []
    for fixture in future:
        if min(len(state.histories[fixture['home']]), len(state.histories[fixture['away']])) < f.MIN_HISTORY:
            skipped.append({**fixture, 'reason': 'insufficient_history'}); continue
        a, b = teams.get(fixture['home']), teams.get(fixture['away'])
        generated = datetime.now(timezone.utc)
        if generated >= f.utc(fixture['at']): continue
        predictions.append({**fixture, 'generatedAt': generated.isoformat(), 'informationCutoff': boundary.isoformat(),
                            'homeAssociation': a, 'awayAssociation': b, 'mappedCrossLeague': a is not None and b is not None and a != b,
                            'eloGap': gaps[fixture['id']], 'models': prediction(state, fixture, model, candidate, gaps[fixture['id']]),
                            'modelSHA256': frozen['sha256']['candidate.json']})
    result = {'candidateFrozenAt': frozen['candidateFrozenAt'], 'predictions': predictions, 'skipped': skipped,
              'offsetEstimate': estimator, 'historyAsOf': boundary.isoformat(), 'autoActivation': False,
              'note': 'Immutable pre-kickoff forecasts. Re-run --score-prospective after full time. No automatic scheduler or production changes.'}
    path.write_text(json.dumps(result, allow_nan=False))
    (output / 'prospective-manifest.json').write_text(json.dumps({
        'savedAt': datetime.now(timezone.utc).isoformat(), 'predictionsSHA256': digest(path),
        'fixtures': len(predictions), 'mappedCrossLeagueFixtures': sum(p['mappedCrossLeague'] for p in predictions),
        'allSavedBeforeKickoff': all(f.utc(p['generatedAt']) < f.utc(p['at']) for p in predictions),
        'firstKickoff': min((p['at'] for p in predictions), default=None),
        'lastKickoff': max((p['at'] for p in predictions), default=None)}, indent=2))
    return result


def score_prospective(output):
    path = output / 'prospective-predictions.json'
    source = load_archive(output); now = datetime.now(timezone.utc); records = []; pending = []
    for row in source['predictions']:
        if f.utc(row['generatedAt']) >= f.utc(row['at']): raise ValueError('Prediction was not recorded before kickoff')
        if f.utc(row['at']) + timedelta(hours=3) > now:
            pending.append(row['id']); continue
        payload = f._request(f'fixtures/{row["id"]}', {'include': 'participants;state;scores'})
        match = f._compact(payload.get('data') or {})
        if match is None:
            pending.append(row['id']); continue
        records.append({**match, 'models': row['models'], 'crossLeague': row['mappedCrossLeague']})
    metrics = {name: main_metrics(records, name) for name in ('current', 'league_offset_refit')}
    cross = [row for row in records if row['crossLeague']]
    result = {'crossLeagueMetrics': {name: main_metrics(cross, name) for name in ('current', 'league_offset_refit')},
              'crossLeagueCompleted': len(cross), 'crossLeagueArchived': sum(row['mappedCrossLeague'] for row in source['predictions']),
              'pairedCrossLeagueUncertainty': paired_bootstrap(cross, 'league_offset_refit'),
              'evaluatedAt': now.isoformat(), 'completed': len(records), 'pending': pending, 'metrics': metrics,
              'modelsRefitted': False, 'activationPerformed': False, 'sourceSHA256': digest(path)}
    (output / 'prospective-results.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('/tmp/scoutwise-league-offset-fresh'))
    parser.add_argument('--training-source', type=Path, default=Path('/tmp/scoutwise-refined-activation/training-data.json'))
    parser.add_argument('--score-prospective', action='store_true')
    args = parser.parse_args(); args.output.mkdir(parents=True, exist_ok=True)
    if args.score_prospective: score_prospective(args.output); return
    frozen = freeze(args.output, args.training_source)
    print('FROZEN', frozen['candidateFrozenAt'], 'production baseline', frozen['activeBaselineId'], flush=True)
    model, source, teams, prior = load_context(args.output)
    candidate = json.loads((args.output / 'candidate.json').read_text())
    fresh = collect_settled(args.output, model, frozen)
    print('NEW COMPLETED', len(fresh['matches']), flush=True)
    merged = sorted(source['matches'] + fresh['matches'], key=lambda m: (f.utc(m['at']), m['id']))
    gaps, _ = causal_gaps(merged, teams, prior)
    state = c.History()
    for m in source['matches']: state.consume(m)
    latest = f.utc(frozen['trainingAsOf']); records = []; skipped = []
    previous_ids = {m['id'] for m in source['matches']}
    old_tests = json.loads(Path('/tmp/scoutwise-league-offset-review/records.json').read_text())['records']
    previous_ids |= {m['id'] for m in old_tests}
    for fixture in fresh['matches']:
        if fixture['id'] in previous_ids: raise ValueError('Fresh holdout overlaps training or earlier test fixtures')
        boundary = daily_boundary(f.utc(fixture['at']))
        if boundary > latest:
            for settled in merged:
                if f.utc(settled['at']) + timedelta(hours=3) <= boundary: state.consume(settled)
            latest = boundary
        if min(len(state.histories[fixture['home']]), len(state.histories[fixture['away']])) < f.MIN_HISTORY:
            skipped.append({'id': fixture['id'], 'reason': 'insufficient_history'}); continue
        a, b = teams.get(fixture['home']), teams.get(fixture['away'])
        records.append({**fixture, 'models': prediction(state, fixture, model, candidate, gaps[fixture['id']]),
                        'european': fixture['league'] in EUROPE, 'crossLeague': fixture['league'] in EUROPE and a is not None and b is not None and a != b})
    active_after, _ = active_record()
    if active_after != frozen['activeBaselineId']: raise ValueError('Production active model changed externally')
    result = summarize_holdout(args.output, records, skipped, fresh, frozen, active_after)
    print('HOLDOUT', json.dumps({'evaluated': len(records), 'crossLeague': result['cohorts']['cross_league']['models']['current']['matches'],
                               'metrics': result['cohorts']['all']['models'], 'gate': result['originalPromotionGate']}), flush=True)
    future = archive_upcoming(args.output, model, candidate, source, teams, prior, fresh, frozen)
    print('ARCHIVED', len(future['predictions']), 'future fixtures;', sum(p['mappedCrossLeague'] for p in future['predictions']), 'mapped cross league', flush=True)
    (args.output / 'verification.json').write_text(json.dumps({
        'trainingAndNewHoldoutDisjoint': not (previous_ids & {m['id'] for m in fresh['matches']}),
        'baselineControlMatchesActiveProduction': frozen['baselineControlMatchesProduction'],
        'frozenArtifactChecksumsValid': all(digest(args.output / name) == value for name, value in frozen['sha256'].items()),
        'prospectivePredictionsBeforeKickoff': all(f.utc(p['generatedAt']) < f.utc(p['at']) for p in future['predictions']),
        'productionModelUnchanged': active_record()[0] == frozen['activeBaselineId'],
        'activationPerformed': False}, indent=2))


if __name__ == '__main__':
    main()
