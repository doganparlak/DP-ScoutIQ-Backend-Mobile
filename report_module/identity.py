"""Stable player identity for report input and document retrieval."""
from decimal import Decimal, InvalidOperation
from sqlalchemy import text


def report_sportmonks_id(identity):
    for key in ('sportmonksId', 'sportmonksPlayerId', 'sportmonks_player_id'):
        value = identity.get(key)
        if value is None:
            continue
        try:
            number = Decimal(str(value))
            if not number.is_finite() or number <= 0 or number != number.to_integral_value():
                raise ValueError('Invalid SportMonks player ID')
            return int(number)
        except (InvalidOperation, TypeError):
            raise ValueError('Invalid SportMonks player ID') from None
    return None


def fetch_report_player(db, provider_id):
    rows = db.execute(text("""
        SELECT id, metadata, content FROM player_data
        WHERE CASE WHEN metadata->>'player_id' ~ '^[0-9]+([.]0+)?$'
                   THEN trunc((metadata->>'player_id')::numeric)::text END = :provider_id
        LIMIT 2
    """), {'provider_id': str(provider_id)}).mappings().all()
    if len(rows) != 1:
        raise ValueError('The report player could not be uniquely resolved by SportMonks ID.')
    return dict(rows[0])


def owned_report_identity(incoming, favorite):
    result = dict(incoming)
    for key in ('playerId','player_id','clubPlayerId','club_player_id','sportmonksId','sportmonksPlayerId','sportmonks_player_id','name','worldCupMode'):
        result.pop(key, None)
    result['name'] = favorite['name']
    for key in ('nationality','gender','team','league','age','height','weight'):
        result[key] = favorite.get(key)
    if favorite.get('player_id') is not None:
        result['sportmonksId'] = favorite['player_id']
    return result
