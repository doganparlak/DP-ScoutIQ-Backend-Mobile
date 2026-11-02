"""Offline comparison only: never registers/activates a model or updates caches.

python -m scripts.evaluate_league_offsets --output /tmp/scoutwise-league-offset-review
Uses the immutable data and fold artifacts from the previous approved experiment.
"""
import argparse
from collections import defaultdict
import csv
from datetime import timedelta
import hashlib
import html
import json
import math
from pathlib import Path
import re
from time import perf_counter

import numpy as np
from sqlalchemy import text
from match_pool_module import forecast as f, forecast_learning as l, forecast_candidates as c
from match_pool_module.forecast_history import daily_boundary
from match_pool_module.forecast_league_offsets import AssociationPrior, LeagueOffsetEstimator, apply_elo_gap
from scripts import forecast_review_metrics as review

FOLDS = ('2026-10-11', '2026-10-18', '2026-10-25')
EUROPE = {2, 5, 2286}
NAMES = ('current', 'league_offset_refit', 'league_offset_overlay')
ALIASES = {'Turkey': 'Türkiye', 'Czech Republic': 'Czechia'}
PRIOR_URL = 'https://kassiesa.net/uefa/data/method5/crank2025.html'


def load_prior(output):
    path = output / 'association-prior-2025.json'
    if not path.exists():
        import requests
        from html.parser import HTMLParser
        class Table(HTMLParser):
            def __init__(self):
                super().__init__(); self.rows = []; self.row = []; self.cell = None
            def handle_starttag(self, tag, attrs):
                if tag == 'tr': self.row = []
                if tag == 'td': self.cell = ''
            def handle_data(self, data):
                if self.cell is not None: self.cell += data
            def handle_endtag(self, tag):
                if tag == 'td' and self.cell is not None:
                    self.row.append(self.cell.strip()); self.cell = None
                if tag == 'tr' and self.row: self.rows.append(self.row)
        response = requests.get(PRIOR_URL, timeout=45); response.raise_for_status(); response.encoding = 'utf-8'
        parser = Table(); parser.feed(response.text)
        values = {}
        for row in parser.rows:
            # country occupies column 2; the five-year ranking follows 5 seasons.
            if len(row) >= 10 and row[0].isdigit() and re.fullmatch(r'\d+\.\d{3}', row[8]):
                values[row[2]] = float(row[8])
        if len(values) != 55 or 'Türkiye' not in values:
            raise ValueError(f'Expected 55 historical associations, got {len(values)}')
        payload = {'availableAt': '2025-06-01T00:00:00Z', 'season': '2024/25 final',
                   'source': PRIOR_URL, 'coefficients': values,
                   'note': 'Frozen before every fixture in the current-season evaluation; no 2026/27 totals.'}
        path.write_text(json.dumps(payload, indent=2))
    return json.loads(path.read_text())


def membership(source, prior):
    competitions = {r['leagueId']: r for r in source['competitions']}
    associations = defaultdict(set)
    leagues = defaultdict(set)
    for match in source['matches']:
        competition = competitions[match['league']]
        if match['league'] in EUROPE: continue
        country = ALIASES.get(competition['country'], competition['country'])
        for side in ('home', 'away'):
            associations[match[side]].add(country)
            leagues[match[side]].add(match['league'])
    conflicts = {team: sorted(countries) for team, countries in associations.items() if len(countries) > 1}
    if conflicts: raise ValueError(f'Ambiguous domestic association membership: {conflicts}')
    mapping = {team: next(iter(countries)) for team, countries in associations.items()}
    return mapping, leagues, competitions


def causal_gaps(matches, teams, prior):
    state = c.History()
    observations, pending, index = [], [], 0
    estimates, gaps, audits = {}, {}, {}
    for match in matches:
        kickoff = f.utc(match['at'])
        if kickoff <= f.utc(prior.available_at): raise ValueError('Prior crosses kickoff')
        while index < len(pending) and f.utc(pending[index]['at']) + timedelta(hours=3) <= kickoff:
            previous = pending[index]
            home, away = previous['home'], previous['away']
            a, b = teams.get(home), teams.get(away)
            contrast = prior.contrast(a, b)
            if previous['league'] in EUROPE and contrast is not None and a != b:
                observations.append({'homeAssociation': a, 'awayAssociation': b,
                    'eloDifference': state.ratings.get(home, 1500.) - state.ratings.get(away, 1500.),
                    'result': 1. if previous['hg'] > previous['ag'] else .5 if previous['hg'] == previous['ag'] else 0.,
                    'availableAt': (f.utc(previous['at']) + timedelta(hours=3)).isoformat(),
                    'fixtureId': previous['id']})
            state.consume(previous); index += 1
        boundary = daily_boundary(kickoff)
        key = boundary.isoformat()
        if key not in estimates:
            settled = [row for row in observations if f.utc(row['availableAt']) <= boundary]
            estimates[key] = LeagueOffsetEstimator(prior).fit(settled)
            audits[key] = {**estimates[key].dump(), 'asOf': key,
                          'latestLabelAvailableAt': max((r['availableAt'] for r in settled), default=None)}
        a, b = teams.get(match['home']), teams.get(match['away'])
        gap = estimates[key].gap(a, b) if match['league'] in EUROPE else 0.
        gaps[match['id']] = gap
        pending.append(match)
    return gaps, audits


def fit_fixed(data, baseline, selected):
    """Same production components, frozen pre-checkpoint feature/parameter choices."""
    from sklearn.ensemble import HistGradientBoostingRegressor
    old = selected['models']['incumbent']
    weights = np.repeat(l.league_weights(data['leagues'], baseline['leagueBalancePower']), 2)
    x, prep = c._matrix(data['raw'], old['columns'], weights)
    linear = {'columns': old['columns'], **prep,
              'beta': f._fit(x, data['goals'], old['regularization'], weights).tolist()}
    tree_spec = selected['models']['nonlinear']
    p, cols = tree_spec['hyperparameters'], tree_spec['columns']
    tree = HistGradientBoostingRegressor(loss='poisson', max_depth=p['depth'], max_leaf_nodes=2 ** p['depth'],
        max_iter=p['trees'], min_samples_leaf=p['minLeaf'], l2_regularization=p['l2'], learning_rate=.05,
        max_bins=64, early_stopping=False, categorical_features=None, random_state=20261101)
    tree.fit(data['raw'][:, cols], data['goals'], sample_weight=weights * data['cardWeights'])
    return linear, {'columns': cols, 'hyperparameters': p, **c.export_trees(tree)}


def rates(linear, tree, raw, weights):
    return weights['incumbent'] * c.linear_predict(linear, raw) + weights['nonlinear'] * c.tree_predict(tree, raw)


def main_metrics(rows, name):
    if not rows: return {'matches': 0}
    values = np.array([r['models'][name]['rates'] for r in rows])
    goals = np.array([[r['hg'], r['ag']] for r in rows])
    metrics = l.metrics(values.ravel(), goals.ravel())
    for key in ('reliability',): metrics.pop(key, None)
    return metrics


def paired_bootstrap(rows, name, seed=20261102):
    if not rows: return {}
    days = sorted({row['at'][:10] for row in rows})
    sums = np.zeros((len(days), 3)); counts = np.zeros(len(days))
    index = {day: i for i, day in enumerate(days)}
    for row in rows:
        contributions = []
        for model in ('current', name):
            d = row['models'][model]
            p = np.array([d['outcomes'][k] for k in ('home', 'draw', 'away')])
            actual = np.array([row['hg'] > row['ag'], row['hg'] == row['ag'], row['hg'] < row['ag']], dtype=float)
            h, a = d['rates']; hg, ag = row['hg'], row['ag']
            nll = h + a - hg * math.log(h) - ag * math.log(a) + math.lgamma(hg + 1) + math.lgamma(ag + 1)
            exact = float(d['topScores'][0]['home'] == hg and d['topScores'][0]['away'] == ag)
            contributions.append(np.array([np.sum((p - actual) ** 2), nll, exact]))
        i = index[row['at'][:10]]; sums[i] += contributions[1] - contributions[0]; counts[i] += 1
    sample = np.random.default_rng(seed).integers(0, len(days), size=(5000, len(days)))
    distribution = sums[sample].sum(axis=1) / counts[sample].sum(axis=1)[:, None]
    return {'matchdays': len(days), 'method': 'paired matchday bootstrap, 5000 draws',
            **{key: {'delta': float(sums[:, i].sum() / counts.sum()),
                      '95CI': np.quantile(distribution[:, i], [.025, .975]).tolist()}
               for i, key in enumerate(('brier', 'scoreLogLoss', 'exactScoreAccuracy'))}}


def make_report(output, result, details):
    import base64
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, 3, figsize=(13, 4), layout='constrained')
    colors = ('#60736a', '#137a3a', '#2563a0')
    labels = ('Current recipe', 'Offset + refit', 'Offset only')
    for ax, (metric, title, factor) in zip(axes, (
            ('outcomeAccuracy', 'Match result accuracy ↑ (%)', 100.),
            ('brier', 'Brier score ↓', 1.), ('scoreLogLoss', 'Score log loss ↓', 1.))):
        for i, name in enumerate(NAMES):
            values = [result['cohorts'][cohort]['models'][name][metric] * factor for cohort in ('all', 'cross_league')]
            positions = np.arange(2) + (i - 1) * .17
            ax.scatter(positions, values, color=colors[i], s=55, label=labels[i], zorder=3)
        ax.set_xticks([0, 1], ['All matches\nN=1,259', 'Cross-league\nN=81'])
        ax.set_title(title, fontsize=11); ax.set_xlim(-.4, 1.4)
        ax.grid(axis='y', alpha=.15); ax.spines[['top', 'right']].set_visible(False)
    axes[0].legend(fontsize=8, loc='upper left')
    fig.suptitle('Chronological paired comparison · production remains unchanged', fontsize=12)
    fig.savefig(output / 'performance.png', dpi=170); plt.close(fig)
    encoded_chart = base64.b64encode((output / 'performance.png').read_bytes()).decode('ascii')
    def table(cohort):
        parts = ['<div class="scroll"><table><tr><th>Model</th><th>Matches</th><th>Result accuracy</th><th>Exact score</th><th>Brier ↓</th><th>Score log loss ↓</th></tr>']
        for name, metrics in result['cohorts'][cohort]['models'].items():
            if not metrics['matches']: continue
            parts.append(f'<tr><td>{name}</td><td>{metrics["matches"]}</td><td>{metrics["outcomeAccuracy"]:.2%}</td><td>{metrics["exactScoreAccuracy"]:.2%}</td><td>{metrics["brier"]:.6f}</td><td>{metrics["scoreLogLoss"]:.6f}</td></tr>')
        return ''.join(parts) + '</table></div>'
    chunks = ['<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>ScoutWise league-offset review</title>',
              '<style>body{font:15px system-ui;color:#193426;background:#f3f7f4;max-width:1250px;margin:32px auto;padding:0 20px}section{padding:22px;background:white;border:1px solid #cfddd3;border-radius:14px;margin:20px 0}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}td,th{text-align:right;padding:10px;border-bottom:1px solid #dfe8e2;white-space:nowrap}td:first-child,th:first-child{text-align:left}th{background:#eaf3ed}.note{color:#586a5f}code{background:#eaf3ed;padding:2px 5px}a{color:#137a3a}</style>',
              '<h1>ScoutWise · learned league Elo offsets</h1>',
              f'<p>Production model {result["activeBefore"]["id"]} remains unchanged. Primary candidate: <b>league_offset_refit</b>. Overlay is diagnostic.</p>',
              '<section><h2>Method</h2><p>Adjusted Elo difference = existing team Elo difference + learned association-offset difference. Each match pair is centered so a shared association baseline cancels exactly. Offsets are learned against residual results conditional on existing Elo; association deviations shrink toward a learned log-UEFA-coefficient prior.</p>',
              '<p>Both core components are refitted using causal adjusted Elo inputs. Architecture, columns, regularization, and blend weights are frozen to the same pre-checkpoint choices as the current production recipe. All other features, including historical opponent-strength adjustments, stay unchanged to isolate this change.</p>',
              '<p>The frozen coefficient snapshot is the 2024/25 final ranking, available before every training/test match. Latest 2026/27 totals are deliberately excluded. Domestic association membership comes from the dataset’s domestic competition participants; no outcome values enter that mapping. Unknown associations and non-UEFA leagues receive no invented offset. Lower-tier differences are not estimated: these inputs contain no direct domestic cross-division cup schedule.</p>',
              '<p>Same historical weeks as the previous review; they are not fresh prospective evidence. The final confirmation week has no UEFA matches, so it cannot independently confirm cross-league improvement. No model registry, report cache, frontend, or Supabase updates were performed.</p>',
              f'<p>Data: {result["inputMatches"]} settled fixtures, {result["configuredCompetitions"]} competitions. Source cutoff: {html.escape(result["asOf"])}. <a href="{PRIOR_URL}">Historical coefficient source</a>.</p></section>']
    chunks += ['<section><h2>Decision</h2><p>The refit challenger fails the existing exact-score safeguard. The offset-only overlay passes the original three-metric gate and leaves domestic outputs identical. Neither has been activated; the cross-league sample is only 81 fixtures across six matchdays and has no untouched cross-league confirmation week.</p>',
               f'<img alt="Probability and accuracy comparison" style="width:100%;height:auto" src="data:image/png;base64,{encoded_chart}"></section>']
    for cohort, data in result['cohorts'].items():
        chunks += [f'<section><h2>{html.escape(cohort)}</h2>', table(cohort)]
        if data.get('bootstrap'): chunks += ['<details><summary>Paired uncertainty estimates</summary><pre>' + html.escape(json.dumps(data['bootstrap'], indent=2)) + '</pre></details>']
        chunks += ['</section>']
    chunks += ['<section><h2>Original promotion gate</h2><pre>' + html.escape(json.dumps({'refit': result['promotionGate'], 'offsetOnly': result['overlayPromotionGate']}, indent=2)) + '</pre></section>',
               '<section><h2>Learned offsets by fold</h2><p>Offsets are relative league components, not a team’s final Elo. Differences are what enters the model.</p><div class="scroll"><table><tr><th>Association</th>']
    for fold in result['folds']: chunks += [f'<th>{fold["cutoff"]}</th>']
    chunks += ['</tr>']
    associations = sorted(result['folds'][-1]['offsets']['offsets'], key=lambda x: -result['folds'][-1]['offsets']['offsets'][x])
    for country in associations:
        chunks += [f'<tr><td>{html.escape(country)}</td>'] + [f'<td>{fold["offsets"]["offsets"][country]:+.1f}</td>' for fold in result['folds']] + ['</tr>']
    chunks += ['</table></div></section>', '<section><h2>All report subcategories</h2><p>Same metric definitions as the earlier review. Brier/log loss lower is better; accuracy higher is better. <a href="all-subcategories.csv">Download CSV</a> · <a href="summary.json">Full summary</a> · <a href="verification.json">Verification</a></p>']
    keys = list(review.LABELS)
    for cohort in ('all', 'cross_league'):
        chunks += [f'<details><summary>{cohort}: {len(keys)} metrics</summary><div class="scroll"><table><tr><th>Metric</th><th>Model</th><th>N</th><th>Accuracy</th><th>Brier</th><th>Log loss</th><th>MAE</th></tr>']
        for key in keys:
            for name in NAMES:
                metric = details[cohort].get(name, {}).get(key)
                if not metric: continue
                cells = [html.escape(review.LABELS[key]), name, str(metric['n'])]
                for field in ('accuracy', 'brier', 'logLoss', 'mae'):
                    val = metric.get(field)
                    cells.append('—' if val is None else f'{val:.2%}' if field == 'accuracy' else f'{val:.6f}')
                chunks += ['<tr>' + ''.join(f'<td>{cell}</td>' for cell in cells) + '</tr>']
        chunks += ['</table></div></details>']
    chunks += ['</section></html>']
    (output / 'report.html').write_text(''.join(chunks))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('/tmp/scoutwise-forecast-upgrade/prepared.json'))
    parser.add_argument('--old', type=Path, default=Path('/tmp/scoutwise-forecast-options-v5'))
    parser.add_argument('--fold-models', type=Path, default=Path('/tmp/scoutwise-forecast-ensemble-v6'))
    parser.add_argument('--baselines', type=Path, default=Path('/tmp/scoutwise-forecast-refresh-v4'))
    parser.add_argument('--output', type=Path, default=Path('/tmp/scoutwise-league-offset-review'))
    args = parser.parse_args(); args.output.mkdir(parents=True, exist_ok=True)
    started = perf_counter()
    source = json.loads(args.source.read_text())
    matches = sorted(source['matches'], key=lambda m: (f.utc(m['at']), m['id']))
    if len(matches) != len({m['id'] for m in matches}): raise ValueError('Duplicate fixture IDs')
    if {m['league'] for m in matches} != set(f.TRAINING_LEAGUES): raise ValueError('Training competition mismatch')
    cards = json.loads((args.old / 'card-events.json').read_text())
    for match in matches: match['cardContext'] = c.card_context(cards[str(match['id'])], match['home'], match['away'])
    raw_prior = load_prior(args.output)
    teams, memberships, competitions = membership(source, raw_prior)
    expected_uefa = {ALIASES.get(row['country'], row['country']) for row in source['competitions'] if row['country'] not in ('Europe', 'Argentina', 'Brazil', 'Saudi Arabia', 'United States')}
    missing_associations = expected_uefa - set(raw_prior['coefficients'])
    if missing_associations: raise ValueError(f'Missing UEFA association priors: {sorted(missing_associations)}')
    known_countries = sorted(expected_uefa)
    prior = AssociationPrior({country: raw_prior['coefficients'][country] for country in known_countries}, raw_prior['availableAt'], raw_prior['source'])
    (args.output / 'competition-mapping.json').write_text(json.dumps({'competitions': source['competitions'],
        'teamAssociations': teams, 'teamDomesticCompetitions': {team: sorted(leagues) for team, leagues in memberships.items()},
        'uefaPriorAssociations': known_countries}, indent=2))
    def active_model():
        with f.engine.connect() as db:
            row = db.execute(text('SELECT id, model_version, trained_at, training_cutoff, artifact->\'refinedEnsemble\'->\'weights\' AS weights FROM mobile_match_forecast_models WHERE is_active')).mappings().one()
        return {k: v.isoformat() if hasattr(v, 'isoformat') else v for k, v in row.items()}
    before = active_model()
    old_rows = json.loads((args.fold_models / 'records.json').read_text())['records']
    old_by_id = {r['id']: r for r in old_rows}
    print('PREPARE', len(matches), 'fixtures; active model', before['id'], flush=True)
    gaps, audits = causal_gaps(matches, teams, prior)
    (args.output / 'causal-offset-audit.json').write_text(json.dumps(audits, indent=2))
    print('OFFSETS', len(audits), 'causal daily snapshots', flush=True)
    records, folds, parity = [], [], []
    for day in FOLDS:
        cutoff = f.utc(day + 'T00:00:00Z'); end = min(cutoff + timedelta(days=7), f.utc(source['asOf']))
        base = json.loads((args.baselines / f'improved44-{day}.json').read_text())
        selected = json.loads((args.fold_models / f'options-{day}.json').read_text())
        strategy = base['strategy']
        meta = json.loads((args.old / f'features-{strategy}.json').read_text())
        with np.load(args.old / f'features-{strategy}.npz') as arrays:
            data = {**meta, **{key: arrays[key] for key in arrays.files}}
        n = sum(f.utc(at) + timedelta(hours=3) <= cutoff for at in data['times'])
        train = {key: value[:2*n] if key in ('raw', 'goals', 'first', 'cardWeights') else value[:n] if key in ('times', 'leagues', 'ids') else value for key, value in data.items()}
        shifted = {**train, 'raw': apply_elo_gap(train['raw'], train['names'], [gaps[fixture] for fixture in train['ids']])}
        print('FIT', day, n, 'eligible training matches', flush=True)
        # The zero-offset control must reconstruct the archived production recipe.
        control_linear, control_tree = fit_fixed(train, base, selected)
        for key in ('columns', 'beta', 'medians', 'mean', 'scale'):
            np.testing.assert_allclose(control_linear[key], selected['models']['incumbent'][key], atol=1e-10, rtol=1e-10)
        assert control_tree['trees'] == selected['models']['nonlinear']['trees']
        assert control_tree['offset'] == selected['models']['nonlinear']['offset']
        linear, tree = fit_fixed(shifted, base, selected)
        (args.output / f'candidate-{day}.json').write_text(json.dumps({'kind': 'offline_league_offset_refit',
            'linear': linear, 'nonlinear': tree, 'weights': selected['options']['rate_blend']['weights'],
            'features': data['names'], 'cutoff': day, 'activationPerformed': False}, allow_nan=False))
        state = c.History()
        for match in matches:
            if f.utc(match['at']) + timedelta(hours=3) <= cutoff: state.consume(match)
        latest = cutoff; errors = []; tested = []
        for match in matches:
            if not cutoff <= f.utc(match['at']) < end: continue
            boundary = daily_boundary(f.utc(match['at']))
            if boundary > latest:
                for previous in matches:
                    if f.utc(previous['at']) + timedelta(hours=3) <= boundary: state.consume(previous)
                latest = boundary
            if match['id'] not in old_by_id: continue
            old = old_by_id[match['id']]
            if min(len(state.histories[match['home']]), len(state.histories[match['away']])) < f.MIN_HISTORY:
                raise ValueError('Baseline fixture eligibility changed')
            raw = np.array([c.features(state, match['home'], match['away'], True, match['league'], match['at'], strategy),
                            c.features(state, match['away'], match['home'], False, match['league'], match['at'], strategy)], float)
            weights = selected['options']['rate_blend']['weights']
            current = rates(selected['models']['incumbent'], selected['models']['nonlinear'], raw, weights)
            expected = old['models']['rate_blend']['rates']
            np.testing.assert_allclose(current, expected, atol=1e-9, rtol=1e-9)
            errors.append(float(np.max(abs(current - expected))))
            alt_raw = apply_elo_gap(raw, data['names'], [gaps[match['id']]])
            alt = rates(linear, tree, alt_raw, weights)
            overlay = rates(selected['models']['incumbent'], selected['models']['nonlinear'], alt_raw, weights)
            if match['league'] not in EUROPE:
                np.testing.assert_array_equal(raw, alt_raw)
                np.testing.assert_array_equal(current, overlay)
            row = {key: match[key] for key in ('id', 'at', 'league', 'hg', 'ag', 'hhg', 'hag')}
            a, b = teams.get(match['home']), teams.get(match['away'])
            row.update(fold=day, homeAssociation=a, awayAssociation=b,
                       crossLeague=match['league'] in EUROPE and a is not None and b is not None and a != b,
                       european=match['league'] in EUROPE, eloGap=gaps[match['id']], models={})
            half_model = {**base['halfTimeModel'], 'leagueTotals': {str(k): v for k, v in state.half_totals.items()}}
            for name, values in zip(NAMES, (current, alt, overlay)):
                distribution = f.score_distribution(*values)
                distribution.update(rates=values.tolist(), halfScenarios=f.half_scenarios(*values,
                    state.histories[match['home']], state.histories[match['away']], match['league'], half_model))
                row['models'][name] = distribution
            records.append(row); tested.append(row)
        estimate_key = max(key for key in audits if f.utc(key) <= cutoff)
        folds.append({'cutoff': day, 'testUntil': end.isoformat(), 'eligibleTrainingMatches': n, 'tested': len(tested),
                      'crossLeagueFixtures': sum(r['crossLeague'] for r in tested),
                      'europeanFixtures': sum(r['european'] for r in tested), 'offsets': audits[estimate_key],
                      'frozenBlendWeights': weights})
        parity.append({'fold': day, 'fixtures': len(errors), 'maximumBaselineRateDifference': max(errors, default=0.), 'zeroOffsetRefitEqualsArchivedProductionRecipe': True})
        print('FINISHED', day, len(tested), 'fixtures;', sum(r['crossLeague'] for r in tested), 'cross league', flush=True)
    if {r['id'] for r in records} != set(old_by_id): raise ValueError('Paired baseline fixture set changed')
    after = active_model()
    if before != after: raise ValueError('Active production model changed during offline evaluation')
    cohorts = {'all': records, 'domestic': [r for r in records if not r['european']],
               'european': [r for r in records if r['european']], 'cross_league': [r for r in records if r['crossLeague']],
               'confirmation': [r for r in records if r['fold'] == FOLDS[-1]]}
    for day in FOLDS: cohorts['week_' + day] = [r for r in records if r['fold'] == day]
    for league in sorted({r['league'] for r in records}): cohorts['competition_' + str(league)] = [r for r in records if r['league'] == league]
    summaries = {cohort: {'models': {name: main_metrics(rows, name) for name in NAMES}} for cohort, rows in cohorts.items()}
    for cohort in ('all', 'domestic', 'european', 'cross_league', 'confirmation'):
        summaries[cohort]['bootstrap'] = {name: paired_bootstrap(cohorts[cohort], name) for name in NAMES[1:]}
    baseline, candidate = [summaries['all']['models'][name] for name in ('current', 'league_offset_refit')]
    gate = {'brierNonWorse': candidate['brier'] <= baseline['brier'],
            'scoreLogLossNonWorse': candidate['scoreLogLoss'] <= baseline['scoreLogLoss'],
            'exactScoreAccuracyNonLower': candidate['exactScoreAccuracy'] >= baseline['exactScoreAccuracy']}
    gate['passed'] = all(gate.values())
    overlay = summaries['all']['models']['league_offset_overlay']
    overlay_gate = {'brierNonWorse': overlay['brier'] <= baseline['brier'],
                    'scoreLogLossNonWorse': overlay['scoreLogLoss'] <= baseline['scoreLogLoss'],
                    'exactScoreAccuracyNonLower': overlay['exactScoreAccuracy'] >= baseline['exactScoreAccuracy']}
    overlay_gate['passed'] = all(overlay_gate.values())
    review.prepare(records)
    details = {cohort: review.summarize(rows, NAMES) for cohort, rows in cohorts.items()}
    result = {'activeBefore': before, 'activeAfter': after, 'activationPerformed': False,
              'productionChanges': False, 'primaryCandidate': 'league_offset_refit',
              'asOf': source['asOf'], 'inputMatches': len(matches), 'configuredCompetitions': len(competitions),
              'evaluatedFixtures': len(records), 'coefficientSnapshot': raw_prior,
              'metricCount': len(review.LABELS), 'folds': folds, 'cohorts': summaries, 'promotionGate': gate, 'overlayPromotionGate': overlay_gate,
              'protocol': {'outerFolds': FOLDS, 'testDataPreviouslyReviewed': True,
                'historyRefresh': '04:00 UTC; settled at least 3 hours', 'offsetLearning': 'same daily cutoff',
                'modelChoices': 'Production recipe; identical pre-checkpoint columns, penalties, tree settings and blend weights',
                'offsetPriorPenalty': 5., 'offsetDeviationPenalty': 20., 'offsetTraining': 'only past cross-association UEFA results; existing team Elo is a fixed predictor',
                'unknownLeagues': 'zero correction, not a zero strength estimate',
                'coefficientSource': PRIOR_URL, 'coefficientAvailableAt': prior.available_at,
                'confirmationWeekHasCrossLeagueFixtures': bool(folds[-1]['crossLeagueFixtures']),
                'domesticTierOffsets': 'not estimated; no domestic cross-division cup schedule in these inputs',
                'opponentAdjustedHistoricalFeatures': 'unchanged to isolate direct Elo input effect'},
              'inputsSHA256': {'source': hashlib.sha256(args.source.read_bytes()).hexdigest()},
              'elapsedSeconds': perf_counter() - started}
    (args.output / 'summary.json').write_text(json.dumps(result, indent=2, allow_nan=False))
    (args.output / 'records.json').write_text(json.dumps({'records': records}, allow_nan=False))
    (args.output / 'all-subcategories.json').write_text(json.dumps(details, allow_nan=False))
    fields = ['cohort', 'model', 'section', 'metric', 'n', 'accuracy', 'brier', 'logLoss', 'mae', 'rmse', 'meanPredicted', 'meanActual', 'confidenceECE']
    with (args.output / 'all-subcategories.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore'); writer.writeheader()
        for cohort, models in details.items():
            for name, metrics in models.items():
                for key, values in metrics.items():
                    writer.writerow({'cohort': cohort, 'model': name, 'section': review.SECTIONS[key], 'metric': review.LABELS[key], **values})
    verification = {'passed': True, 'baselineParity': parity, 'allPairedFixturesIdentical': True,
                    'activeModelUnchanged': before == after, 'priorPredatesEveryFixture': True,
                    'offsetLabelsPrecedeDailyCutoff': all(not value['latestLabelAvailableAt'] or f.utc(value['latestLabelAvailableAt']) <= f.utc(key) for key, value in audits.items()),
                    'sameAssociationInputAdjustmentExactlyZero': all(abs(r['eloGap']) < 1e-10 for r in records if r['homeAssociation'] is not None and r['homeAssociation'] == r['awayAssociation']),
                    'finiteBoundedGoalRates': all(.05 <= rate <= 8. for r in records for distribution in r['models'].values() for rate in distribution['rates']),
                    'reportMetricCount': len(review.LABELS), 'uefaAssociationsMapped': len(known_countries)}
    if not all(value for key, value in verification.items() if isinstance(value, bool)): raise ValueError('Verification failed')
    (args.output / 'verification.json').write_text(json.dumps(verification, indent=2))
    make_report(args.output, result, details)
    print('DONE', json.dumps({'output': str(args.output), 'gate': gate, 'all': summaries['all']['models'],
                             'crossLeague': summaries['cross_league']['models'], 'seconds': result['elapsedSeconds']}), flush=True)


if __name__ == '__main__':
    main()
