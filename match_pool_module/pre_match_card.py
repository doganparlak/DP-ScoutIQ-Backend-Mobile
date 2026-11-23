"""Read-only pre-match cover data. Does not generate or save reports."""
import os
from concurrent.futures import ThreadPoolExecutor
import requests
from .fixtures import SPORTMONKS_BASE_URL, SportMonksError, _get_team_details, fixture_coaches

def build_pre_match_card(fixture_id):
    token = os.getenv('SPORTMONKS_API_KEY')
    if not token:
        raise SportMonksError('Match data unavailable')
    response = requests.get(f'{SPORTMONKS_BASE_URL}/fixtures/{fixture_id}', params={'api_token':token,'include':'participants;league.country;season;round;venue;state;formations;coaches'},timeout=45)
    response.raise_for_status()
    fixture=response.json().get('data') or {}
    if not fixture:
        raise SportMonksError('Match not found')
    coaches = fixture_coaches(fixture.get('coaches'))
    state = fixture.get('state') or {}
    upcoming = (state.get('short_name') or state.get('state')) in {'NS', 'TBA'}

    def team_data(team):
        coach = coaches.get(str(team['id'])) or {}
        name = coach.get('display_name') or coach.get('name')
        image = coach.get('image_path')
        if upcoming and not name:
            try:
                detail = _get_team_details(team['id'])
                name, image = detail.get('coachName'), detail.get('coachImageUrl')
            except (SportMonksError, requests.RequestException, ValueError):
                pass
        return {
            'id': team['id'], 'name': team.get('name'),
            'location': (team.get('meta') or {}).get('location'),
            'image_url': team.get('image_path'),
            'coach_name': name, 'coach_image_url': image,
        }

    participants=fixture.get('participants') or []
    with ThreadPoolExecutor(max_workers=2) as pool:
        teams=list(pool.map(team_data,participants))
    return {'fixture':{'id':fixture_id,'name':fixture.get('name'),'starting_at':fixture.get('starting_at')},'teams':teams,'league':fixture.get('league'),'season':fixture.get('season'),'round':fixture.get('round'),'venue':fixture.get('venue'),'state':fixture.get('state'),'lineups':[],'events':[],'scores':[],'coverage':{'unique_team_metrics':0,'unique_player_metrics':0,'players_with_minutes':0}}
