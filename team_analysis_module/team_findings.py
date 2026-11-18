"""Fixed, multi-metric performance findings over the original selected matches.

Comparisons use matched own/opponent observations or explicit opportunity/outcome
relationships. No league benchmarks, missing-as-zero values, or arbitrary style
scores are used to declare strengths or weaknesses.
"""
from copy import deepcopy
from math import ceil, isfinite
from report_module.profile_evidence import bind_row_evidence, free_profile_snapshot

CATALOG_VERSION = 2
# Each identity has one distinct football scope and its two presentation labels.
CATALOG = {
 'creation': {'strength':('Pozisyon Üretim Gücü','Chance Creation Strength'), 'weakness':('Pozisyon Üretiminde Yetersizlik','Insufficient Chance Creation'), 'description':('Kilit paslar ve net fırsatlar üzerinden pozisyon üretiminin sürekliliği ve rakibe göre dengesi.','Chance creation through key passes and clear opportunities, compared with the opposition.')},
 'finishing': {'strength':('Bitiricilik Verimi','Finishing Efficiency'), 'weakness':('Bitiricilik Sorunu','Finishing Problems'), 'description':('Üretilen fırsatların kalitesi ve şutların gole dönüşümü arasındaki ilişki.','How chance quality and shot conversion translate into goals.')},
 'passing': {'strength':('Pas Oyununda Güven','Passing Security'), 'weakness':('Pas Oyununda Verim Kaybı','Inefficient Passing'), 'description':('Pas hacmi ve pas başarı oranının birlikte ortaya koyduğu top kullanma güvenliği.','Ball-use security through the relationship between passing volume and accuracy.')},
 'progression': {'strength':('Son Bölgeye Erişim Gücü','Final-third Access'), 'weakness':('Son Bölgeye Erişim Sorunu','Limited Final-third Access'), 'description':('Pas oyununun son bölgeye taşınması ve ileri bölgelerde bağlantı kurulması.','How passing reaches the final third and connects play in advanced areas.')},
 'dribbling': {'strength':('Dripling Başarısı','Dribbling Success'), 'weakness':('Driplinglerde Verimsizlik','Inefficient Dribbling'), 'description':('Bireysel girişimlerin başarı oranı ve hücuma sağladığı başarılı aksiyon katkısı.','The success rate of individual attempts and their successful attacking contribution.')},
 'set_pieces': {'strength':('Duran Top Tehdidi','Set-piece Threat'), 'weakness':('Duran Toplarda Üretim Sorunu','Limited Set-piece Production'), 'description':('Duran top fırsatları ile bu fırsatlardan üretilen gol beklentisinin dengesi.','The relationship between set-piece opportunities and the expected goals produced.')},
 'duels': {'strength':('İkili Mücadele Gücü','Duel Strength'), 'weakness':('İkili Mücadele Zafiyeti','Duel Vulnerability'), 'description':('Mücadele yükü, kazanma oranı ve mümkün olduğunda hava topu katkısının birlikte değerlendirilmesi.','Duel workload and success, with aerial contribution where recorded.')},
 'recoveries': {'strength':('Top Kazanım Etkinliği','Effective Ball Recovery'), 'weakness':('Top Kazanımında Yetersizlik','Insufficient Ball Recovery'), 'description':('Top kazanımı ve pas arası katkısının rakibin pas faaliyetine göre değerlendirilmesi.','Recoveries and interceptions assessed against the opposition’s passing activity.')},
 'threat': {'strength':('Rakip Tehdidini Sınırlama','Restricting Opposition Threat'), 'weakness':('Rakip Tehdidine Açıklık','Exposure to Opposition Threat'), 'description':('İzin verilen şut kalitesi ve isabet oranının aynı maçlardaki karşılıklı tehditle birlikte okunması.','Allowed shot quality and accuracy in the context of both sides’ threat in the same matches.')},
 'errors': {'strength':('Savunmada Hata Kontrolü','Defensive Error Control'), 'weakness':('Hata Kaynaklı Savunma Riski','Error-related Defensive Risk'), 'description':('Şut ve golle sonuçlanan kayıtlı hataların oluşturduğu savunma riskinin değerlendirilmesi.','Defensive risk from recorded errors leading to shots and goals.')},
 'discipline': {'strength':('Disiplinli Oyun','Disciplined Play'), 'weakness':('Disiplin Sorunları','Disciplinary Problems'), 'description':('Faul yükü ile sarı ve kırmızı kartların birlikte oluşturduğu disiplin profili.','Disciplinary performance through foul workload and yellow and red cards.')},
 'game_management': {'strength':('Skor Yönetimi','Game-state Management'), 'weakness':('Skor Yönetiminde Kırılganlık','Fragile Game-state Management'), 'description':('Öne geçilen maçları kazanma ve gerideyken eşitliği yeniden kurma kapasitesi.','Converting leads into wins and restoring parity after falling behind.')},
}

ALIASES = {'Expected Goals (xG)':'Expected Goals','Expected Goals on Target (xGoT)':'Expected Goals On Target'}
RATES = {
 'Accurate Passes (%)':('Accurate Passes','Passes'),
 'Goal Conversion (%)':('Goals','Shots Total'),
 'On Target Goal Conversion (%)':('Goals','Shots On Target'),
 'Inside-box Shot Share (%)':('Shots Insidebox','Shots Total'),
 'Outfield Duels Won (%)':('Outfield Duels Won','Outfield Total Duels'),
 'Outfield Final-third Pass Share (%)':('Outfield Passes In Final Third','Outfield Passes'),
 'Dribble Accuracy (%)':('Successful Dribbles','Dribble Attempts'),
 'Duels Won (%)':('Duels Won','Total Duels'),
 'Aerials Won (%)':('Aerials Won','Aerials'),
 'Shots On Target (%)':('Shots On Target','Shots Total'),
 'Shot Quality (%)':('Expected Goals','Shots Total'),
 'Final-third Pass Share (%)':('Passes In Final Third','Passes'),
}
ACTIVITY = {'Recoveries Per 100 Opponent Passes':'Ball Recovery',
            'Interceptions Per 100 Opponent Passes':'Interceptions',
            'Fouls Per 100 Opponent Passes':'Fouls',
            'Outfield Recoveries Per 100 Opponent Passes':'Outfield Ball Recovery',
            'Outfield Interceptions Per 100 Opponent Passes':'Outfield Interceptions'}


def number(value):
    if value is None or value == '' or isinstance(value, bool): return None
    try:
        value = float(value)
        return value if isfinite(value) and value >= 0 else None
    except (TypeError, ValueError): return None


def _values(team, lineups=()):
    from report_module.metric_validation import has_team_statistics, is_standard_count
    covered = has_team_statistics(team.get('categories') or {})
    result = {}
    from .report import TEAM_METRIC_CATEGORY

    for rows in [*(team.get('categories') or {}).values(), team.get('expected_metrics') or [], team.get('extra_metrics') or []]:
        for row in rows:
            original = row.get('name')
            name = ALIASES.get(original, TEAM_METRIC_CATEGORY.get(original, (None, original))[1])
            value = number(row.get('value'))
            if name and value is not None and (not row.get('_inferred_zero') or covered and is_standard_count(name)) and ('%' not in name or value <= 100):
                result[name] = value
    # Recorded outfield contributions are a distinct, explicitly labelled fallback
    # when the provider has no corresponding team total. Never include keepers.
    totals, minutes_by_metric, total_minutes = {}, {}, 0
    for player in lineups:
        if int(player.get('team_id') or 0) != int(team.get('id') or 0): continue
        position = str(player.get('position_name') or '').lower()
        if player.get('position_id') == 24 or 'goalkeeper' in position or 'kaleci' in position: continue
        values = {metric.get('name'):number(metric.get('value')) for rows in (player.get('categories') or {}).values() for metric in rows}
        if not values.get('Minutes Played'): continue
        total_minutes += values['Minutes Played']
        for name in ('Total Duels','Duels Won','Ball Recovery','Interceptions','Passes','Passes In Final Third'):
            if values.get(name) is not None:
                totals[name] = totals.get(name,0)+values[name]
                minutes_by_metric[name] = minutes_by_metric.get(name,0)+values['Minutes Played']
    result.update({'Outfield '+name:value for name,value in totals.items() if total_minutes>0 and minutes_by_metric[name]/total_minutes>=.7})
    if 'Yellow Cards' in result and 'Red Cards' in result:
        result['Total Cards'] = result['Yellow Cards'] + result['Red Cards']
    return result


class Evidence:
    def __init__(self, reports, team_id):
        self.matches, seen = [], set()
        for index, report in enumerate(reports):
            fid = (report.get('fixture') or {}).get('id') or f'snapshot:{index}'
            if fid in seen: continue
            seen.add(fid)
            own = next((t for t in report.get('teams') or [] if int(t.get('id') or 0) == int(team_id)), None)
            opponents = [t for t in report.get('teams') or [] if int(t.get('id') or 0) != int(team_id)]
            if own is not None and len(opponents) == 1:
                self.matches.append((_values(own,report.get('lineups') or []), _values(opponents[0],report.get('lineups') or [])))
        self.minimum = max(min(3,len(self.matches)),ceil(len(self.matches)*.6))

    def observation(self, match, side, name):
        values, opponent = match[side], match[1-side]
        if name in RATES:
            a,b = RATES[name]
            if a in values and b in values:
                if values[b] > 0 and values[a] <= values[b]:
                    return values[a], values[b], 100
                return None  # No opportunity is not a zero success rate.
            if name in values: return values[name],1,1
            return None
        if name in ACTIVITY:
            a = ACTIVITY[name]
            if a in values and opponent.get('Passes',0)>0:
                return values[a],opponent['Passes'],100
            return None
        if name in values: return values[name],1,1
        return None

    def cell(self, name, side, observations):
        numerator = sum(row[0] for row in observations)
        denominator = sum(row[1] for row in observations)
        scale = observations[0][2]
        value = numerator/denominator*scale
        label = ('Opponent ' if side else '')+name
        rate = name in RATES
        shown = f'{value:.2f}'.rstrip('0').rstrip('.')
        return {'label':label,'value':f'%{shown}' if rate else shown,'contributes':True,
                'basis':'rate' if rate else 'per100OpponentPasses' if name in ACTIVITY else 'perMatch',
                'matchesCovered':len(observations), 'sampleSize':len(self.matches),
                'detail':f'{numerator:g} / {denominator:g}' if scale == 100 else '', '_number':value}

    def paired(self, name, inverse=False, weight=1):
        pairs = [(self.observation(m,0,name),self.observation(m,1,name)) for m in self.matches]
        pairs = [(a,b) for a,b in pairs if a is not None and b is not None and a[2]==b[2]]
        if name in RATES and pairs:
            counted=[p for p in pairs if p[0][2]==100]
            reported=[p for p in pairs if p[0][2]==1]
            pairs=counted if len(counted)>=self.minimum else reported
        if not pairs or len(pairs)<self.minimum: return None
        a,b = self.cell(name,0,[p[0] for p in pairs]),self.cell(name,1,[p[1] for p in pairs])
        denominator = abs(a['_number'])+abs(b['_number'])
        score = (a['_number']-b['_number'])/denominator if denominator else 0
        return {'score':-score if inverse else score,'weight':weight,'cells':[a,b],
                'comparison':'lower own value is preferable' if inverse else 'higher own value is preferable',
                'coverage':len(pairs)/len(self.matches)}

    def internal(self, first, second, weight=1, require_full=False):
        pairs = [(self.observation(m,0,first),self.observation(m,0,second)) for m in self.matches]
        pairs = [(a,b) for a,b in pairs if a is not None and b is not None]
        if not pairs or len(pairs)<self.minimum or require_full and len(pairs)!=len(self.matches): return None
        a,b=self.cell(first,0,[p[0] for p in pairs]),self.cell(second,0,[p[1] for p in pairs])
        denominator=abs(a['_number'])+abs(b['_number'])
        return {'score':(a['_number']-b['_number'])/denominator if denominator else 0,
                'weight':weight,'cells':[a,b],'comparison':f'{first} compared with {second} in the same matches',
                'coverage':len(pairs)/len(self.matches)}


def _game_management(profile):
    states={row['key']:row for row in (profile or {}).get('states') or []}
    lead,behind=states.get('ahead',{}),states.get('behind',{})
    comparisons=[]
    configs=[(lead,'Lead Win Rate (%)','Lead Dropped Points Rate (%)',lead.get('finalWins'),
              (lead.get('finalDraws',0)+lead.get('finalLosses',0)) if 'finalDraws' in lead and 'finalLosses' in lead else None),
             (behind,'Trailing Equalizer Rate (%)','Trailing No Equalizer Rate (%)',behind.get('matchesToLevel'),
              behind.get('matchesEntered',0)-behind['matchesToLevel'] if 'matchesToLevel' in behind else None)]
    for state,first,second,a,b in configs:
        count=state.get('matchesEntered',0)
        if count<3 or a is None or b is None or not 0<=a<=count or not 0<=b<=count: continue
        cells=[]
        for name,value in [(first,a),(second,b)]:
            cells.append({'label':name,'value':f'%{value/count*100:.2f}'.rstrip('0').rstrip('.'),
                          'contributes':True,'basis':'rate','matchesCovered':count,'sampleSize':count,
                          'detail':f'{value} / {count}', '_number':value/count*100})
        comparisons.append({'score':(a-b)/count,'weight':1,'cells':cells,
                            'comparison':'conditional outcomes in distinct matches entering this state','coverage':1})
    return comparisons if len(comparisons)==2 else []


def _candidates_from_evidence(evidence,score_flow=None,include_relative=False):
    if not evidence.matches: return []
    pair=evidence.paired
    # Two comparisons per scope produce a coherent, multi-metric four-cell finding.
    configs={
      'creation':[pair('Key Passes'),pair('Big Chances Created')],
      'finishing':[pair('On Target Goal Conversion (%)') or evidence.internal('Goals','Expected Goals',require_full=True),pair('Goal Conversion (%)')],
      'passing':[pair('Accurate Passes (%)',weight=2),pair('Passes',weight=.35)],
      'progression':[pair('Final-third Pass Share (%)') or pair('Outfield Final-third Pass Share (%)'),pair('Passes In Final Third') or pair('Outfield Passes In Final Third')],
      'dribbling':[pair('Dribble Accuracy (%)',weight=2),pair('Successful Dribbles',weight=.35)],
      'set_pieces':[pair('Expected Goals Set Play (xGSP)',weight=2),pair('Corners',weight=.35)],
      'duels':[pair('Duels Won (%)',weight=2) or pair('Outfield Duels Won (%)',weight=2),pair('Aerials Won (%)') or pair('Duels Won',weight=.35) or pair('Outfield Duels Won',weight=.35)],
      'recoveries':[pair('Recoveries Per 100 Opponent Passes') or pair('Outfield Recoveries Per 100 Opponent Passes'),pair('Interceptions Per 100 Opponent Passes') or pair('Outfield Interceptions Per 100 Opponent Passes')],
      'threat':[pair('Shot Quality (%)') or pair('Inside-box Shot Share (%)'),pair('Shots On Target (%)')],
      'errors':[pair('Error Lead To Shot',inverse=True),pair('Error Lead To Goal',inverse=True)],
      'discipline':[pair('Fouls Per 100 Opponent Passes',inverse=True),pair('Total Cards',inverse=True) or pair('Yellow Cards',inverse=True)],
      'game_management':_game_management(score_flow),
    }
    result=[]
    for identity,comparisons in configs.items():
        if len(comparisons)!=2 or any(c is None for c in comparisons): continue
        weight=sum(c['weight'] for c in comparisons)
        score=sum(c['score']*c['weight'] for c in comparisons)/weight
        side='strength' if score>0 else 'weakness'
        # Efficiency-led topics require the efficiency signal to agree with the conclusion.
        efficiency_agrees = identity not in {'finishing','passing','dribbling','duels','set_pieces'} or comparisons[0]['score']*score>0
        supporting=sum(c['weight'] for c in comparisons if c['score']*score>0)/weight
        qualified = abs(score)>=.025 and efficiency_agrees and supporting>=.6
        if not qualified and not include_relative: continue
        coverage=min(c['coverage'] for c in comparisons)
        cells={cell['label']:{k:v for k,v in cell.items() if k!='_number'} for c in comparisons for cell in c['cells']}
        if len(cells)<3: continue
        result.append({'findingId':identity,'findingSide':side,'score':round(abs(score)*coverage,5),
                       'source_metrics':cells,'comparisons':comparisons,'sampleSize':len(evidence.matches),
                       'relativeScore':score*coverage,'qualified':qualified})
    return sorted(result,key=lambda row:(-row['score'],row['findingId']))


def candidates(reports,team_id,score_flow=None,include_relative=False):
    evidence=Evidence(reports,team_id)
    result=_candidates_from_evidence(evidence,score_flow,include_relative)
    for row in result:
        row.update(findingScope='selected_matches',supportingMatches=len(evidence.matches))
    # Missing-data coverage is allowed and disclosed per cell. Outcome-based
    # subsets are not: every candidate is evaluated against the selected sample.
    return result


def select_findings(reports,team_id,side,score_flow=None,confirmed=None):
    if side not in {'strength','weakness'}: raise ValueError('Unknown finding side')
    chosen=[]
    confirmed=(confirmed or {}).get('themes') or []
    excluded={row.get('findingId') for row in confirmed}
    ranked=candidates(reports,team_id,score_flow,include_relative=side=='weakness')
    for item in ranked:
        if not item['qualified']: continue
        if item['findingSide']!=side or item['findingId'] in excluded: continue
        labels=set(item['source_metrics'])
        others=[*chosen,*confirmed]
        if any(len(labels & set(row.get('source_metrics') or {m['label']:m for m in row.get('metrics') or []}))/max(1,len(labels))>.65 for row in others): continue
        chosen.append(item)
        if len(chosen)==2: break
    if side == 'weakness' and len(chosen)<2:
        # Fill from the least favorable supported comparisons, without
        # presenting a relative ranking as a confirmed adverse finding.
        used=excluded | {row['findingId'] for row in chosen}
        for item in sorted(ranked,key=lambda row:(row['relativeScore'],row['findingId'])):
            if item['findingId'] in used: continue
            chosen.append({**item,'findingSide':'weakness','relativeFallback':True})
            if len(chosen)==2: break
    return chosen


def bind_findings(selected,lang):
    result=[]
    for candidate in selected:
        identity,side=candidate['findingId'],candidate['findingSide']
        definition=CATALOG[identity]
        bound=bind_row_evidence({'findingId':identity,'findingSide':side,
            'relativeFallback':candidate.get('relativeFallback',False),
            'findingScope':candidate.get('findingScope','selected_matches'),
            'supportingMatches':candidate.get('supportingMatches',candidate['sampleSize']),
            'sampleSize':candidate['sampleSize'],
            'title':definition[side][0 if lang=='tr' else 1],
            'description':definition['description'][0 if lang=='tr' else 1]},candidate['source_metrics'],4)
        bound['metrics']=list(deepcopy(candidate['source_metrics']).values())
        bound['evidence_values']=deepcopy(candidate['source_metrics'])
        result.append(bound)
    return {'themes':result,'catalogVersion':CATALOG_VERSION}



def build_findings(reports, team_id, side, lang, score_flow, confirmed=None):
    selected = select_findings(reports, team_id, side, score_flow, confirmed)
    return free_profile_snapshot(bind_findings(selected, lang))
