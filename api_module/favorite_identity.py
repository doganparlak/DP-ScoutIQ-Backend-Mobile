"""Resolve portfolio saves to a validated current player and stable provider ID."""
from decimal import Decimal, InvalidOperation
from sqlalchemy import text
from matchup_module.comparison import _fetch_player_by_sportmonks_id, _fetch_player_metadata


def sportmonks_id(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0 or number != number.to_integral_value():
            return None
        return int(number)
    except (InvalidOperation, ValueError, TypeError):
        return None


def resolve_favorite_player(db, payload):
    if payload.sportmonksId is not None:
        row = _fetch_player_by_sportmonks_id(db, payload.sportmonksId)
        return {"id": row["id"], "metadata": row["content"]}
    if payload.playerId:
        try:
            row = _fetch_player_metadata(db, payload.playerId, bool(payload.worldCupMode))
        except ValueError:
            row = None
        if row:
            meta = row['content']
            name = str(meta.get('player_name') or meta.get('name') or '').strip().casefold()
            nationality = str(meta.get('nationality_name') or meta.get('nationality') or '').strip().casefold()
            provider_id = sportmonks_id(meta.get('player_id'))
            if name == payload.name.strip().casefold() and (not payload.nationality or nationality == payload.nationality.strip().casefold()) and provider_id:
                current = _fetch_player_by_sportmonks_id(db, provider_id)
                return {"id": current['id'], "metadata": current['content']}
    rows = db.execute(text("""
        SELECT id, metadata FROM player_data
        WHERE lower(trim(COALESCE(metadata->>'player_name', metadata->>'name', ''))) = lower(trim(:name))
          AND (:nationality = '' OR lower(trim(COALESCE(metadata->>'nationality_name', metadata->>'nationality', ''))) = lower(trim(:nationality)))
          AND (:gender = '' OR lower(trim(COALESCE(metadata->>'gender', ''))) = lower(trim(:gender)))
        LIMIT 2
    """), {'name': payload.name, 'nationality': payload.nationality or '', 'gender': payload.gender or ''}).mappings().all()
    if len(rows) != 1 or sportmonks_id(rows[0]['metadata'].get('player_id')) is None:
        raise ValueError('This player cannot be uniquely identified. Please select the player again from Player Pool.')
    current = _fetch_player_by_sportmonks_id(db, sportmonks_id(rows[0]['metadata']['player_id']))
    return {'id': current['id'], 'metadata': current['content']}
