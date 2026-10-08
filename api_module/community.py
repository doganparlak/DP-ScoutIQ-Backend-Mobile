"""One permanent display name shared by mobile community activities."""
import unicodedata

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


def community_available(db):
    return bool(db.execute(text("""SELECT EXISTS (SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='users' AND column_name='community_nickname')""")).scalar())


def save_community_nickname(db, user_id, nickname):
    nick = unicodedata.normalize('NFC', ' '.join(str(nickname or '').split()))
    if not 2 <= len(nick) <= 24 or any(unicodedata.category(c).startswith('C') for c in nick):
        raise ValueError('Nickname must contain 2–24 printable characters')
    current = db.execute(text('SELECT community_nickname FROM public.users WHERE id=:uid FOR UPDATE'), {'uid': user_id}).first()
    if not current:
        raise ValueError('User not found')
    if current[0]:
        return {'nickname': current[0]}
    try:
        db.execute(text('UPDATE public.users SET community_nickname=:nick WHERE id=:uid'), {'uid': user_id, 'nick': nick})
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ValueError('Nickname is already taken') from None
    return {'nickname': nick}
