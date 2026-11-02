"""Additional chronological generalization audit; no production writes.

Earlier windows are fixed by fixture coverage, not by prediction performance.
This checks model-development robustness; it cannot prove absence of overfitting.
"""
import argparse
from datetime import timedelta
import json
from pathlib import Path
from time import perf_counter
import numpy as np
from match_pool_module import forecast as f, forecast_candidates as c, forecast_refined as refined
from match_pool_module.forecast_history import daily_boundary
from match_pool_module.forecast_league_offsets import apply_elo_gap
from scripts.evaluate_league_offsets import fit_fixed, rates, main_metrics, paired_bootstrap

FOLDS = ('2026-08-09', '2026-08-16', '2026-08-23', '2026-09-13')
NAMES = ('current', 'league_offset_refit', 'league_offset_overlay')
EUROPE = {2, 5, 2286}


def summarize_pairs(values, matches):
    return {name: main_metrics([{'hg': m['hg'], 'ag': m['ag'], 'models': {name: {'rates': values[name][i]}}}
                               for i, m in enumerate(matches)], name) for name in NAMES}


def stability(rows):
    if not rows: return {}
    deltas = []
    for row in rows:
        losses = []
        for name in ('current', 'league_offset_refit'):
            h, a = row['models'][name]['rates']
            losses.append(h + a - row['hg'] * np.log(h) - row['ag'] * np.log(a))
        deltas.append(float(losses[1] - losses[0]))
    values = np.asarray(deltas)
    days = sorted({r['at'][:10] for r in rows})
    return {'matches': len(rows), 'fractionLowerScoreLoss': float(np.mean(values < 0)),
            'meanLossDelta': float(values.mean()), 'medianLossDelta': float(np.median(values)),
            'afterRemovingBestFiveGains': float(np.sort(values)[min(5, len(values)-1):].mean()),
            'leaveOneMatchdayOut': {day: float(np.mean([v for r, v in zip(rows, values) if r['at'][:10] != day]))
                                    for day in days if sum(r['at'][:10] != day for r in rows)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('/tmp/scoutwise-league-offset-generalization'))
    args = parser.parse_args(); args.output.mkdir(parents=True, exist_ok=True)
    start = perf_counter()
    source = json.loads(Path('/tmp/scoutwise-forecast-upgrade/prepared.json').read_text())
    matches = sorted(source['matches'], key=lambda m: (f.utc(m['at']), m['id']))
    cards = json.loads(Path('/tmp/scoutwise-forecast-options-v5/card-events.json').read_text())
    for match in matches: match['cardContext'] = c.card_context(cards[str(match['id'])], match['home'], match['away'])
    audits = json.loads(Path('/tmp/scoutwise-league-offset-review/causal-offset-audit.json').read_text())
    teams = {int(k): v for k, v in json.loads(Path('/tmp/scoutwise-league-offset-review/competition-mapping.json').read_text())['teamAssociations'].items()}
    def gap(match):
        if match['league'] not in EUROPE: return 0.
        offsets = audits[daily_boundary(f.utc(match['at'])).isoformat()]['offsets']
        a, b = teams.get(match['home']), teams.get(match['away'])
        return offsets[a] - offsets[b] if a in offsets and b in offsets else 0.
    by_id = {m['id']: m for m in matches}
    previous = json.loads(Path('/tmp/scoutwise-league-offset-review/records.json').read_text())['records']
    previous_ids = {r['id'] for r in previous}
    records, folds = [], []
    for day in FOLDS:
        cutoff = f.utc(day + 'T00:00:00Z'); end = cutoff + timedelta(days=7)
        train = [m for m in matches if f.utc(m['at']) + timedelta(hours=3) <= cutoff]
        model_path = args.output / f'baseline-{day}.json'
        print('TRAIN FROM SCRATCH', day, len(train), flush=True)
        if model_path.exists(): base = json.loads(model_path.read_text())
        else:
            base = f.fit_model(train)
            if base is None: raise ValueError('Insufficient historical training examples')
            base['asOf'] = cutoff.isoformat()
            model_path.write_text(json.dumps(base, allow_nan=False))
        data = c.examples(train, base['strategy'])
        selected = {'models': {'incumbent': {'columns': [data['names'].index(k) for k in base['features']], 'regularization': base['regularization']},
                               'nonlinear': base['refinedEnsemble']['nonlinear']}}
        current_linear, current_tree = fit_fixed(data, base, selected)
        for key in ('beta', 'medians', 'mean', 'scale'):
            np.testing.assert_allclose(current_linear[key], base[key], atol=1e-9, rtol=1e-9)
        assert current_tree['trees'] == base['refinedEnsemble']['nonlinear']['trees']
        current_linear['calibration'] = base['goalRateCalibration']
        shifted = {**data, 'raw': apply_elo_gap(data['raw'], data['names'], [gap(by_id[i]) for i in data['ids']])}
        candidate_linear, candidate_tree = fit_fixed(shifted, base, selected)
        candidate_linear['calibration'] = base['goalRateCalibration']
        weights = base['refinedEnsemble']['weights']
        # In-sample metrics quantify relative optimism, not unbiased performance.
        train_rates = {'current': rates(current_linear, current_tree, data['raw'], weights),
                       'league_offset_refit': rates(candidate_linear, candidate_tree, shifted['raw'], weights),
                       'league_offset_overlay': rates(current_linear, current_tree, shifted['raw'], weights)}
        training = summarize_pairs({k: v.reshape(-1, 2) for k, v in train_rates.items()}, [by_id[i] for i in data['ids']])
        state = data['state']; latest = cutoff; tested = []; skipped = 0
        for fixture in matches:
            if not cutoff <= f.utc(fixture['at']) < end: continue
            if fixture['id'] in previous_ids: raise ValueError('New audit window overlaps previous test fixtures')
            boundary = daily_boundary(f.utc(fixture['at']))
            if boundary > latest:
                for settled in matches:
                    if f.utc(settled['at']) + timedelta(hours=3) <= boundary: state.consume(settled)
                latest = boundary
            if min(len(state.histories[fixture['home']]), len(state.histories[fixture['away']])) < f.MIN_HISTORY:
                skipped += 1; continue
            raw = np.array([c.features(state, fixture['home'], fixture['away'], True, fixture['league'], fixture['at'], base['strategy']),
                            c.features(state, fixture['away'], fixture['home'], False, fixture['league'], fixture['at'], base['strategy'])], float)
            alternative = apply_elo_gap(raw, data['names'], [gap(fixture)])
            predictions = {'current': rates(current_linear, current_tree, raw, weights),
                           'league_offset_refit': rates(candidate_linear, candidate_tree, alternative, weights),
                           'league_offset_overlay': rates(current_linear, current_tree, alternative, weights)}
            a, b = teams.get(fixture['home']), teams.get(fixture['away'])
            row = {k: fixture[k] for k in ('id', 'at', 'league', 'hg', 'ag')}
            row.update(fold=day, crossLeague=fixture['league'] in EUROPE and a is not None and b is not None and a != b,
                       european=fixture['league'] in EUROPE, homeAssociation=a, awayAssociation=b, models={})
            for name, values in predictions.items():
                row['models'][name] = {**f.score_distribution(*values), 'rates': values.tolist()}
            records.append(row); tested.append(row)
        all_metrics = {name: main_metrics(tested, name) for name in NAMES}
        cross = [r for r in tested if r['crossLeague']]
        cross_metrics = {name: main_metrics(cross, name) for name in NAMES}
        optimism = {name: {'trainScoreLogLoss': training[name]['scoreLogLoss'], 'testScoreLogLoss': all_metrics[name]['scoreLogLoss'],
                           'generalizationGap': all_metrics[name]['scoreLogLoss'] - training[name]['scoreLogLoss']} for name in NAMES}
        fold = {'cutoff': day, 'trainingMatches': len(train), 'eligibleTrainingMatches': len(data['ids']), 'tested': len(tested),
                'crossLeague': len(cross), 'skipped': skipped, 'strategy': base['strategy'], 'weights': weights,
                'train': training, 'test': all_metrics, 'crossLeagueMetrics': cross_metrics, 'optimism': optimism}
        folds.append(fold)
        (args.output / 'progress.json').write_text(json.dumps({'folds': folds}, indent=2))
        (args.output / 'records.json').write_text(json.dumps(records, allow_nan=False))
        print('TESTED', day, json.dumps({'all': all_metrics, 'cross': cross_metrics, 'gap': optimism}), flush=True)
    cohorts = {'additional_windows': records, 'additional_cross_league': [r for r in records if r['crossLeague']],
               'combined_historical': records + previous, 'combined_cross_league': [r for r in records + previous if r['crossLeague']]}
    result = {'method': 'Nested chronological walk-forward. Every core model fitted from scratch before its weekly test; no random split.',
              'newAuditWindows': FOLDS, 'offsetHyperparametersChanged': False, 'newFixtureOverlapPreviousTests': False,
              'historicalModelDevelopmentSelectionBiasStillPossible': True,
              'folds': folds, 'cohorts': {}, 'activationPerformed': False, 'elapsedSeconds': perf_counter() - start}
    for key, rows in cohorts.items():
        result['cohorts'][key] = {'models': {name: main_metrics(rows, name) for name in NAMES},
                                 'bootstrapRefit': paired_bootstrap(rows, 'league_offset_refit'), 'stabilityRefit': stability(rows)}
    result['weeklyScoreLossWins'] = sum(fold['test']['league_offset_refit']['scoreLogLoss'] < fold['test']['current']['scoreLogLoss'] for fold in folds)
    result['weeklyBrierWins'] = sum(fold['test']['league_offset_refit']['brier'] < fold['test']['current']['brier'] for fold in folds)
    (args.output / 'summary.json').write_text(json.dumps(result, indent=2, allow_nan=False))
    print('DONE', json.dumps({'seconds': result['elapsedSeconds'], 'weeklyScoreLossWins': result['weeklyScoreLossWins'],
                              'weeklyBrierWins': result['weeklyBrierWins'], 'additional': result['cohorts']['additional_cross_league']['models']}), flush=True)


if __name__ == '__main__':
    main()
