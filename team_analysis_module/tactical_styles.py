"""Use the coach classifier over the team's original recorded match snapshots."""
from copy import deepcopy
from math import ceil, sqrt
from statistics import mean
from itertools import combinations, product

from .style_evidence import numeric

STYLE_VERSION = 2


def _metric_options(style, catalog, side):
    from .style_catalog import BUNDLES
    signals = sorted(style['signals'],key=lambda signal:-(signal['weight']*signal['support']*signal['coverage']))
    strengths = {signal['metric']:signal['weight']*signal['support']*signal['coverage'] for signal in signals}
    core = {signal['metric'] for signal in style.get('distinctiveSignals') or []}
    if not core:
        core = {signal['metric'] for signal in signals[:2]}
    keys = list(dict.fromkeys([signal['metric'] for signal in signals] + BUNDLES[side][style['id']][0]))
    keys = [key for key in keys if key in catalog]
    options = []
    for chosen in combinations(keys,4):
        # Provider team totals and their outfield fallback cannot fill two cells.
        if not core.issubset(chosen) or len({key.removeprefix('Outfield ') for key in chosen})!=4:
            continue
        quality = sum(strengths.get(key,0) for key in chosen)
        options.append((chosen,quality))
    if not options:
        return []
    best = max(quality for _,quality in options)
    # Distinctness must not displace the defining evidence with weak filler.
    return sorted((option for option in options if option[1]>=best*.9),key=lambda option:(-option[1],option[0]))


def _four_metric_cells(keys,catalog):
    source = {}
    for key in keys:
        metric = catalog[key]
        value = metric['seasons'][0]['value']
        shown = f'{value:.2f}'.rstrip('0').rstrip('.')
        basis = 'rate' if metric['percentage'] else 'per100OpponentPasses' if 'Per 100 Opponent Passes' in key else 'per100Passes' if key.endswith(' Per 100 Passes') else 'perMatch'
        source[metric['name']] = {'label':metric['name'],'value':f'%{shown}' if metric['percentage'] else shown,
                                  'contributes':True,'sourceKey':key,'basis':basis,
                                  'matchesCovered':metric['seasons'][0]['covered']}
    return source


def _distinct_metric_sets(options):
    def objective(pair):
        sets = [{key.removeprefix('Outfield ') for key in keys} for keys,_ in pair]
        repeated = sum(len(a & b) for i,a in enumerate(sets) for b in sets[i+1:])
        return repeated,-sum(quality for _,quality in pair),tuple(tuple(keys) for keys,_ in pair)
    return min(product(*options),key=objective)


def select_team_styles(reports, team_id, lang='en', side='attack', single_match=False):
    if side not in {'attack', 'defence'}:
        raise ValueError('Unknown profile side')
    from . import style_evidence as evidence
    from .style_catalog import ATTACK, DEFENCE, DESCRIPTIONS
    registry = ATTACK if side == 'attack' else DEFENCE
    from .style_classification import rank_styles, distinct_attack_candidate
    meta, samples, seen = {}, [], set()
    players = evidence.PlayerSamples(team_id, meta)
    for index, report in enumerate(reports):
        fid = (report.get('fixture') or {}).get('id') or f'snapshot:{index}'
        if fid in seen:
            continue
        seen.add(fid)
        teams = report.get('teams') or []
        if not any(int(team.get('id') or 0) == team_id for team in teams):
            continue
        stats = []
        for team in teams:
            sources = {**(team.get('categories') or {}), 'expected': team.get('expected_metrics') or [], 'extra': team.get('extra_metrics') or []}
            for rows in sources.values():
                for row in rows:
                    if not row.get('_inferred_zero') and numeric(row.get('value')) is not None:
                        stats.append({'participant_id':team['id'], 'type':{'name':row.get('name')}, 'data':{'value':row['value']}})
        sample = evidence.extract_match({'participants':[{'id':team['id']} for team in teams], 'statistics':stats}, team_id, meta)
        outfield = []
        for row in report.get('lineups') or []:
            if int(row.get('team_id') or 0) != team_id:
                continue
            values = {metric['name']:numeric(metric.get('value')) for rows in (row.get('categories') or {}).values()
                      for metric in rows if metric.get('name') and not metric.get('_inferred_zero')}
            minutes = values.get('Minutes Played')
            if minutes is None or minutes <= 0:
                continue
            player = {'id':row.get('player_id') or row.get('player_name'), 'teamId':team_id,
                      'name':row.get('player_name') or '', 'position':row.get('position_name') or '',
                      'positionId':row.get('position_id'), 'minutes':minutes,
                      'metrics':{name:value for name,value in values.items() if name in evidence.PLAYER_METRICS and value is not None and value >= 0}}
            if evidence.role_name(player) != 'goalkeeper':
                outfield.append(player)
        players.add({'id':fid, 'players':outfield})
        sample.update(players.samples[fid])
        evidence.add_context_rates(sample, meta)
        samples.append(sample)
    if not samples:
        return []
    catalog = {}
    for key, definition in meta.items():
        if evidence.FORBIDDEN.search(key):
            continue
        observed = [s for s in samples if key in s]
        if len(observed) < max(min(5, len(samples)), ceil(len(samples) * .7)):
            continue
        value = evidence.pool(observed, [key], meta)[0]['value']
        values = [s[key][0] / s[key][1] * (100 if definition['percentage'] and key != 'Ball Possession %' else 1) for s in observed]
        average = mean(values)
        variation = sqrt(mean((v-average)**2 for v in values)) / max(abs(average), .001)
        catalog[key] = {**definition, 'seasons':[{'value':value, 'covered':len(observed), 'matches':len(samples), 'matchVariation':variation,'matchValues':values}]}
    selected, selected_fits, metric_options = [], [], []
    for style in rank_styles(catalog, side, secondary_match=single_match):
        if style['coverage'] <= 0 or style['statisticalFit'] <= 0:
            continue
        if side == 'attack' and not distinct_attack_candidate(style, selected_fits):
            continue
        options = _metric_options(style,catalog,side)
        if not options:
            continue  # Four relevant recorded metrics are required; never invent a fourth.
        source = _four_metric_cells(options[0][0],catalog)
        identity = style['id']
        selected.append({'styleId':identity, 'styleSide':side, 'title':registry[identity][0 if lang=='tr' else 1],
                         'description':DESCRIPTIONS[side][identity][0 if lang=='tr' else 1],
                         'statisticalFit':style['statisticalFit'], 'evidenceCoverage':style['coverage'],
                         'source_metrics':source, 'signals':style['signals'],
                         'secondaryTendency':style.get('secondaryTendency',False),
                         'readingBoundary':registry[identity][2] + (' This is a secondary tendency, not a dominant style. Explicitly describe it as a secondary attacking route, including its measured limitations; do not imply successful execution.' if style.get('secondaryTendency') else '')})
        selected_fits.append(style)
        metric_options.append(options)
        if len(selected) == 2:
            break
    if selected:
        for style,(keys,_) in zip(selected,_distinct_metric_sets(metric_options)):
            style['source_metrics'] = _four_metric_cells(keys,catalog)
    return selected


def bind_selected_styles(draft, selected):
    """Keep classifier identity and authoritative cells fixed through generation/editing."""
    from report_module.profile_evidence import bind_row_evidence
    rows = draft.get('themes') or []
    result = []
    for style in selected:
        row = next((row for row in rows if row.get('styleId') == style['styleId']), None)
        if row is None:
            row = {}
        metadata = {key:style[key] for key in ('styleId','styleSide','title','description','statisticalFit','evidenceCoverage')}
        source = style['source_metrics']
        bound = bind_row_evidence({**row, **metadata}, source, 4)
        # All displayed cells explain the style selection, including valid recorded zeroes.
        bound['metrics'] = list(deepcopy(source).values())
        bound['evidence_values'] = deepcopy(source)
        result.append(bound)
    return result
