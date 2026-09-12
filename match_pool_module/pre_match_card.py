"""Read-only pre-match cover data. Does not generate or save reports."""
import os
from concurrent.futures import ThreadPoolExecutor
import requests
from .fixtures import SPORTMONKS_BASE_URL, SportMonksError

def build_pre_match_card(fixture_id):
    token = os.getenv('SPORTMONKS_API_KEY')
    if not token:
        raise SportMonksError('Match data unavailable')
    response = requests.get(f'{SPORTMONKS_BASE_URL}/fixtures/{fixture_id}', params={'api_token':token,'include':'participants;league.country;season;round;venue;state;formations;coaches'},timeout=45)
    response.raise_for_status()
    fixture=response.json().get('data') or {}
    if not fixture:
        raise SportMonksError('Match not found')
    def team_data(team):
        count=None
        try:
            r=requests.get(f"{SPORTMONKS_BASE_URL}/squads/teams/{team['id']}/extended",params={'api_token':token,'per_page':250},timeout=20)
            r.raise_for_status()
            count=len({p['id'] for p in r.json().get('data') or [] if p.get('in_squad') is True and p.get('id')})
        except (requests.RequestException,ValueError):
            pass
        return {'id':team['id'],'name':team.get('name'),'location':(team.get('meta') or {}).get('location'),'image_url':team.get('image_path'),'player_count':count}
    participants=fixture.get('participants') or []
    with ThreadPoolExecutor(max_workers=2) as pool:
        teams=list(pool.map(team_data,participants))
    return {'fixture':{'id':fixture_id,'name':fixture.get('name'),'starting_at':fixture.get('starting_at')},'teams':teams,'league':fixture.get('league'),'season':fixture.get('season'),'round':fixture.get('round'),'venue':fixture.get('venue'),'state':fixture.get('state'),'lineups':[],'events':[],'scores':[],'coverage':{'unique_team_metrics':0,'unique_player_metrics':0,'players_with_minutes':0}}
