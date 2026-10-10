"""Internal prize contact messages; never send mail to the supplied contact address."""
import os
import smtplib
import json
import threading
import time
from pathlib import Path
from datetime import date, timedelta, datetime, timezone
from email.message import EmailMessage
import google.auth
from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession

from api_module.utilities import settings

_cache = None
_cache_until = 0
_lock = threading.Lock()


def get_week_prizes(week):
    """Read the trusted client-template default, never prize amounts from a request."""
    global _cache, _cache_until
    with _lock:
        if _cache is None or time.monotonic() >= _cache_until:
            project = os.getenv('FIREBASE_REMOTE_CONFIG_PROJECT_ID', 'scoutwise-prod')
            credential_path = os.getenv('FIREBASE_REMOTE_CONFIG_CREDENTIALS') or os.getenv('GOOGLE_PLAY_SERVICE_ACCOUNT_JSON', '/etc/secrets/play_service_account.json')
            if not Path(credential_path).is_file() and Path('play_service_account.json').is_file():
                credential_path = 'play_service_account.json'
            scopes = ['https://www.googleapis.com/auth/firebase.remoteconfig']
            credentials = (service_account.Credentials.from_service_account_file(credential_path, scopes=scopes)
                           if Path(credential_path).is_file() else google.auth.default(scopes=scopes)[0])
            with AuthorizedSession(credentials) as session:
                response = session.get(f'https://firebaseremoteconfig.googleapis.com/v1/projects/{project}/remoteConfig', timeout=10)
                response.raise_for_status()
                template = response.json()
            parameters = dict(template.get('parameters', {}))
            for group in template.get('parameterGroups', {}).values():
                parameters.update(group.get('parameters', {}))
            raw = parameters.get('score_prediction_prizes', {}).get('defaultValue', {}).get('value', '{"weeks":{}}')
            weeks = json.loads(raw).get('weeks', {})
            if not isinstance(weeks, dict):
                raise ValueError('Invalid weekly prize configuration')
            _cache = {'weeks': weeks, 'firebaseVersion': template.get('version', {}).get('versionNumber')}
            _cache_until = time.monotonic() + 300
        entry = _cache['weeks'].get(str(week))
        if not isinstance(entry, dict) or entry.get('enabled') is False:
            raise ValueError('No prizes configured for this competition')
        for key in ('first', 'second', 'third'):
            value = entry.get(key)
            valid = (isinstance(value, str) and bool(value.strip()) and len(value) <= 500) or (
                isinstance(value, dict) and any(isinstance(value.get(lang), str) and value[lang].strip() for lang in ('tr', 'en'))
                and all(not value.get(lang) or isinstance(value[lang], str) and len(value[lang]) <= 500 for lang in ('tr', 'en')))
            if not valid:
                raise ValueError('Incomplete weekly prize configuration')
        return json.loads(json.dumps({key: entry[key] for key in ('first', 'second', 'third')} | {
            'weekStart': str(week), 'firebaseVersion': _cache['firebaseVersion'], 'capturedAt': datetime.now(timezone.utc).isoformat()}))


def format_prize(value):
    if isinstance(value, str):
        return value
    return value.get('tr') or value.get('en') or ''


def send_prize_claim_email(*, user_id, nickname, week, rank, contact_email, phone, prizes):
    config = settings['email']
    sender = config['sender_email']
    recipient = os.environ.get('PRIZE_CLAIM_EMAIL', '').strip() or sender
    if not sender or not config['sender_password'] or not recipient:
        raise RuntimeError('Prize claim email is not configured')
    message = EmailMessage()
    message['From'] = sender
    message['To'] = recipient
    start = date.fromisoformat(str(week))
    match_dates = f'{start + timedelta(days=4)} – {start + timedelta(days=7)}'
    message['Subject'] = f'[ScoutWise Ödül Talebi] {match_dates} · {rank}. sıra'
    if contact_email:
        message['Reply-To'] = contact_email
    message.set_content(
        f'Haftalık Skor Tahmin Ligi ödül talebi\n\n'
        f'Kullanıcı ID: {user_id}\nTakma ad: {nickname}\nYarışma haftası ID: {week}\nMaç tarihleri: {match_dates}\n'
        f'Kesinleşen sıra: {rank}\nİletişim e-postası: {contact_email or "Paylaşılmadı"}\nTelefon: {phone or "Paylaşılmadı"}\n\n'
        'Bu yarışmanın tam ödül listesi (arşivlenen kayıt):\n'
        f'Birincilik: {format_prize(prizes["first"])}\n'
        f'İkincilik: {format_prize(prizes["second"])}\n'
        f'Üçüncülük: {format_prize(prizes["third"])}\n\n'
        f'Kullanıcının kazandığı ödül: {format_prize(prizes[{1: "first", 2: "second", 3: "third"}[rank]])}\n'
        f'Firebase yapılandırma sürümü: {prizes.get("firebaseVersion") or "Kaydedilmedi"}\n'
        f'Ödül listesinin kayıt zamanı: {prizes.get("capturedAt") or "Kaydedilmedi"}\n\n'
        'Ödülü yukarıdaki arşivlenmiş listeye göre manuel olarak tanımlayın.\n'
        'İletişim bilgileri yalnızca ödül işlemleri için paylaşılmıştır.\n'
    )
    with smtplib.SMTP(config['smtp_server'], config['smtp_port'], timeout=15) as server:
        server.starttls()
        server.login(sender, config['sender_password'])
        server.send_message(message)
