"""Read-only, resumable Sportmonks collection for offline forecast experiments."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from match_pool_module import forecast as f


def collect(league, season, output, cutoff):
    directory=output/'pages'/str(season['id']);directory.mkdir(parents=True,exist_ok=True)
    rows=[];raw_count=0;excluded=0
    for page in range(1,101):
        path=directory/f'{page}.json'
        if path.exists():
            saved=json.loads(path.read_text())
        else:
            for attempt in range(4):
                try:
                    payload=f._request('fixtures',{'filters':f'fixtureSeasons:{season["id"]};fixtureStates:5',
                        'include':'participants;state;scores;statistics.type;xGFixture.type;pressure;events',
                        'per_page':100,'page':page})
                    break
                except RuntimeError:
                    if attempt==3:raise
                    time.sleep(2**attempt)
            fixtures=payload.get('data')or[];compact=[];skipped=0
            for fixture in fixtures:
                if fixture.get('season_id')!=season['id'] or fixture.get('league_id')!=league['leagueId']:
                    raise ValueError('Sportmonks season/league filter mismatch')
                if f.utc(fixture['starting_at'])+__import__('datetime').timedelta(hours=3)>cutoff or fixture.get('length') not in (90,None):
                    skipped+=1;continue
                match=f._compact(fixture)
                if match is None:skipped+=1;continue
                match['seasonStart']=season['starting_at'];match['seasonName']=season['name']
                compact.append(match)
            saved={'raw':len(fixtures),'excluded':skipped,'matches':compact,
                   'hasMore':bool((payload.get('pagination')or{}).get('has_more'))}
            path.write_text(json.dumps(saved,allow_nan=False))
        rows.extend(saved['matches']);raw_count+=saved['raw'];excluded+=saved['excluded']
        if not saved['hasMore']:break
    else:raise ValueError('Fixture pagination safety limit')
    result={'leagueId':league['leagueId'],'name':league['name'],'country':league['country'],
            'seasonId':season['id'],'seasonName':season['name'],'startsAt':season['starting_at'],
            'matches':len(rows),'rawFixtures':raw_count,'excluded':excluded}
    (output/'seasons'/f'{season["id"]}.json').write_text(json.dumps({'competition':result,'matches':rows},allow_nan=False))
    return result,rows


def main():
    p=argparse.ArgumentParser();p.add_argument('--coverage',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--cutoff',default='2026-11-04T00:00:00Z');args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=True);(args.output/'seasons').mkdir(exist_ok=True)
    coverage=json.loads(args.coverage.read_text());cutoff=f.utc(args.cutoff);results=[];matches={}
    tasks=[(league,season) for league in coverage['leagues'] for season in league['seasons']]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures=[pool.submit(collect,l,s,args.output,cutoff)for l,s in tasks]
        for future in as_completed(futures):
            meta,rows=future.result();results.append(meta)
            for row in rows:
                if row['id'] in matches and matches[row['id']]!=row:raise ValueError('Conflicting duplicate fixture')
                matches[row['id']]=row
            (args.output/'collection-progress.json').write_text(json.dumps({'seasons':len(results),'fixtures':len(matches),'completed':results},indent=2))
            print('COLLECT',len(results),'/',len(tasks),meta['country'],meta['name'],meta['seasonName'],len(rows),'TOTAL',len(matches),flush=True)
    source={'asOf':args.cutoff,'collectedAt':datetime.now(timezone.utc).isoformat(),'competitions':results,
            'matches':sorted(matches.values(),key=lambda m:(f.utc(m['at']),m['id']))}
    (args.output/'dataset.json').write_text(json.dumps(source,allow_nan=False))
    print('DONE',len(matches),'fixtures',len(results),'competition-seasons',flush=True)

if __name__=='__main__':main()
