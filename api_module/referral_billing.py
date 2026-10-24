"""Non-blocking referral analytics, isolated from the entitlement/payment path."""
import datetime as dt
from decimal import Decimal
import hashlib
import json
import logging
from uuid import uuid5, NAMESPACE_URL
from zoneinfo import ZoneInfo
from sqlalchemy import text
from api_module.database import SessionLocal
from api_module.referral_store_events import PLANS, StoreEvent, collect_apple, collect_google

logger = logging.getLogger(__name__)
REPORT_TIMEZONE = 'Europe/Istanbul'


def subscription_key(platform, external_id):
    # Purchase tokens are credentials; analytics only stores their hash.
    return platform + ':' + hashlib.sha256(external_id.encode()).hexdigest()


def freeze_attribution(db, platform=None, external_id=None):
    db.execute(text('''WITH legacy AS MATERIALIZED (
        SELECT u.id FROM users u WHERE u.referral_code IS NOT NULL AND u.referral_code_set_at IS NULL
          AND EXISTS (SELECT 1 FROM subscription_entitlements e WHERE e.last_seen_user_id=u.id
            AND e.referral_attribution_locked_at IS NULL AND (:platform IS NULL OR e.platform=:platform)
            AND (:external_id IS NULL OR e.external_id=:external_id))
      ), dated AS (
        UPDATE users u SET referral_code_set_at=NOW() FROM legacy l WHERE u.id=l.id RETURNING u.id
      ) UPDATE subscription_entitlements e
      SET referral_code=CASE WHEN u.referral_code_set_at<=e.created_at OR u.id IN (SELECT id FROM legacy) THEN u.referral_code END,
          referral_attributed_since=CASE WHEN u.referral_code_set_at<=e.created_at OR u.id IN (SELECT id FROM legacy)
                                    THEN COALESCE(u.referral_code_set_at,NOW()) END,
          referral_attribution_locked_at=NOW()
      FROM users u WHERE e.last_seen_user_id=u.id AND e.referral_attribution_locked_at IS NULL
        AND (:platform IS NULL OR e.platform=:platform)
        AND (:external_id IS NULL OR e.external_id=:external_id)'''),
      {'platform': platform, 'external_id': external_id})


def load_snapshot(db, platform, external_id):
    return db.execute(text('''SELECT platform,external_id,last_seen_user_id AS user_id,
      referral_code,referral_attributed_since AS since,referral_subscription_id
      FROM subscription_entitlements WHERE platform=:platform AND external_id=:external_id'''),
      {'platform': platform, 'external_id': external_id}).mappings().first()


def record_events(db, snapshot, events, linked_token=None):
    """One atomic, replay-safe ledger batch; original attribution never changes."""
    if not snapshot or not snapshot.get('referral_code') or not snapshot.get('user_id') or not snapshot.get('since'):
        return 0
    platform, external = snapshot['platform'], snapshot['external_id']
    db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'), {'key': 'referral-user:' + str(snapshot['user_id'])})
    current = load_snapshot(db, platform, external)
    if current is None or current['user_id'] != snapshot['user_id']:
        return 0
    snapshot = dict(current)
    root = snapshot['referral_subscription_id'] or subscription_key(platform, external)
    if linked_token and platform == 'android':
        freeze_attribution(db, platform, linked_token)
        prior = load_snapshot(db, platform, linked_token)
        if prior and prior['user_id'] == snapshot['user_id']:
            root = prior['referral_subscription_id'] or subscription_key(platform, linked_token)
            # A verified Google replacement inherits its original subscription's
            # attribution, including an explicitly unattributed subscription.
            snapshot['referral_code'], snapshot['since'] = prior['referral_code'], prior['since']
    db.execute(text('''UPDATE subscription_entitlements SET referral_subscription_id=:root,
      referral_code=:code,referral_attributed_since=:since WHERE platform=:platform AND external_id=:external'''),
      {'root': root, 'code': snapshot['referral_code'], 'since': snapshot['since'], 'platform': platform, 'external': external})
    if not snapshot.get('referral_code') or not snapshot.get('since'):
        return 0
    count = 0
    for event in sorted(events, key=lambda e: (e.occurred_at, e.event_type != 'subscription_charge', e.transaction_id)):
        if event.product_id not in PLANS or event.occurred_at < snapshot['since']:
            continue
        # Never credit a refund to a partner that did not receive the charge.
        if event.event_type == 'subscription_refund':
            charge_id = event.transaction_id.split(':refund:', 1)[0]
            found = db.execute(text('''SELECT 1 FROM referral_events WHERE platform=:p AND store_event_id=:id
              AND event_type='subscription_charge' AND referral_code=:code'''),
              {'p': platform, 'id': ('sandbox:' if event.is_test else 'production:') + charge_id, 'code': snapshot['referral_code']}).scalar()
            if not found:
                continue
        store_id = ('sandbox:' if event.is_test else 'production:') + event.transaction_id
        if platform == 'ios' and event.event_type == 'subscription_charge' and event.metadata.get('revocation_status') == 'none' and event.metadata.get('apple_signed_at', 0):
            # A newer signed Apple transaction without a revocation proves a
            # refund reversal. Older/out-of-order notifications cannot undo it.
            reversal = db.execute(text("""UPDATE referral_events SET metadata=metadata || CAST(:state AS jsonb)
              WHERE platform='ios' AND event_type='subscription_refund'
                AND split_part(store_event_id,':refund:',1)=:store AND referral_code=:code
                AND COALESCE((metadata->>'apple_signed_at')::bigint,0)<:signed"""),
              {'store': store_id, 'code': snapshot['referral_code'], 'signed': event.metadata['apple_signed_at'],
               'state': json.dumps({'reversed': True, 'apple_signed_at': event.metadata['apple_signed_at']})})
            count += reversal.rowcount
        key = platform + ':' + event.event_type + ':' + store_id
        kind = event.kind
        if event.event_type == 'subscription_charge' and kind == 'first_purchase' and event.amount is not None and event.amount > 0:
            prior_paid = db.execute(text("""SELECT plan,billing_period_end FROM referral_events
              WHERE user_id=:uid AND is_test=:test AND event_type='subscription_charge' AND amount>0
                AND occurred_at<:at ORDER BY occurred_at DESC LIMIT 1"""),
                {'uid': snapshot['user_id'], 'test': event.is_test, 'at': event.occurred_at}).mappings().first()
            if prior_paid:
                kind = 'reactivation' if prior_paid['billing_period_end'] and prior_paid['billing_period_end'] < event.occurred_at else 'plan_change'
        metadata = {**event.metadata, 'product_id': event.product_id,
                    'pricing_status': 'reported' if event.amount is not None else 'unavailable',
                    'purchase_key': subscription_key(platform, external)}
        result = db.execute(text('''INSERT INTO referral_events
          (event_id,user_id,referral_code,event_type,platform,occurred_at,is_test,
           store_event_id,plan,amount,currency,metadata,subscription_id,charge_kind,billing_period_start,billing_period_end)
          VALUES (:event_id,:uid,:code,:type,:platform,:at,:test,:store,:plan,:amount,:currency,
                  CAST(:metadata AS jsonb),:subscription,:kind,:start,:end)
          ON CONFLICT (platform,store_event_id,event_type)
           WHERE event_type IN ('subscription_charge','subscription_refund') DO UPDATE
          SET amount=COALESCE(EXCLUDED.amount,referral_events.amount),
              currency=COALESCE(EXCLUDED.currency,referral_events.currency),
              metadata=EXCLUDED.metadata
          WHERE (referral_events.amount IS NULL AND EXCLUDED.amount IS NOT NULL)
             OR (EXCLUDED.platform='ios' AND EXCLUDED.event_type='subscription_refund'
                 AND COALESCE((EXCLUDED.metadata->>'apple_signed_at')::bigint,0)
                     > COALESCE((referral_events.metadata->>'apple_signed_at')::bigint,0))
          RETURNING event_id'''),
          {'event_id': str(uuid5(NAMESPACE_URL, key)), 'uid': snapshot['user_id'], 'code': snapshot['referral_code'],
           'type': event.event_type, 'platform': platform, 'at': event.occurred_at, 'test': event.is_test,
           'store': store_id, 'plan': PLANS[event.product_id], 'amount': event.amount, 'currency': event.currency,
           'metadata': json.dumps(metadata), 'subscription': root,
           'kind': kind if event.event_type == 'subscription_charge' else None,
           'start': event.period_start, 'end': event.period_end})
        count += bool(result.scalar())
    return count


def refresh_monthly_reports(db):
    """Rebuild derived counts from the immutable live ledger, by local month.

    Revenue is gross customer payments by currency, not store payout. Free trial
    periods, tests, unknown prices and manual grants never count as paid users.
    """
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('referral-monthly',0))"))
    events = db.execute(text('''SELECT referral_code,user_id,event_type,occurred_at,amount,currency,charge_kind,
      store_event_id FROM referral_events WHERE NOT is_test AND COALESCE(metadata->>'reversed','false')<>'true' ORDER BY occurred_at,event_id''')).mappings().all()
    reports, first_paid, trial_users = {}, set(), set()
    for e in events:
        month = e['occurred_at'].astimezone(ZoneInfo(REPORT_TIMEZONE)).date().replace(day=1)
        key = (month, e['referral_code'])
        report = reports.setdefault(key, {'new_paid_users': set(), 'renewing_users': set(), 'renewal_payments': 0,
                 'reactivated_users': set(), 'plan_change_payments': 0, 'unclassified_payments': 0,
                 'charges': 0, 'unpriced_charges': 0, 'unpriced_refunds': 0, 'free_periods': 0, 'currencies': {}, 'ad_requests': 0, 'ad_impressions': 0})
        if e['event_type'] in ('ad_request', 'ad_impression'):
            report['ad_requests' if e['event_type'] == 'ad_request' else 'ad_impressions'] += 1
            continue
        amount = e['amount']
        if e['event_type'] == 'subscription_refund' and amount is None:
            report['unpriced_refunds'] += 1
        if e['event_type'] == 'subscription_charge':
            report['charges'] += 1
            if amount is None:
                report['unpriced_charges'] += 1
                continue
            if amount == 0:
                report['free_periods'] += 1
                if e['user_id'] is not None:
                    trial_users.add((e['referral_code'], e['user_id']))
                continue
            identity = e['user_id'] if e['user_id'] is not None else 'deleted:' + e['store_event_id']
            # First positive verified payment, even if the store called it a
            # renewal after a free trial. Never count a renewal as a new user.
            first_key = (e['referral_code'], identity)
            if first_key not in first_paid:
                if e['charge_kind'] == 'first_purchase' or first_key in trial_users:
                    report['new_paid_users'].add(identity)
                first_paid.add(first_key)
            kind = e['charge_kind']
            if kind == 'renewal':
                report['renewing_users'].add(identity)
                report['renewal_payments'] += 1
            elif kind == 'reactivation':
                report['reactivated_users'].add(identity)
            elif kind == 'plan_change':
                report['plan_change_payments'] += 1
            elif kind not in ('first_purchase',):
                report['unclassified_payments'] += 1
        if amount is not None and e['currency']:
            totals = report['currencies'].setdefault(e['currency'], {'gross_charges': Decimal(0), 'refunds': Decimal(0)})
            totals['gross_charges' if e['event_type'] == 'subscription_charge' else 'refunds'] += amount
    for (month, code), report in reports.items():
        counts = {key: len(report[key]) if isinstance(report[key], set) else report[key]
                  for key in ('new_paid_users','renewing_users','renewal_payments','reactivated_users','plan_change_payments','unclassified_payments')}
        summary = {key: report[key] for key in ('charges','unpriced_charges','unpriced_refunds','free_periods','ad_requests','ad_impressions')}
        summary['currencies'] = {currency: {**{k: str(v) for k,v in amounts.items()},
                       'net_gross': str(amounts['gross_charges']-amounts['refunds'])} for currency,amounts in report['currencies'].items()}
        summary['basis'] = 'verified gross customer payments; tests and manual grants excluded'
        db.execute(text('''INSERT INTO referral_monthly_reports
          (month,referral_code,report_timezone,summary,new_paid_users,renewing_users,renewal_payments,
           reactivated_users,plan_change_payments,unclassified_payments)
          VALUES (:month,:code,:tz,CAST(:summary AS jsonb),:new_paid_users,:renewing_users,:renewal_payments,
                  :reactivated_users,:plan_change_payments,:unclassified_payments)
          ON CONFLICT (month,referral_code,report_timezone) DO UPDATE SET summary=EXCLUDED.summary,
          new_paid_users=EXCLUDED.new_paid_users,renewing_users=EXCLUDED.renewing_users,
          renewal_payments=EXCLUDED.renewal_payments,reactivated_users=EXCLUDED.reactivated_users,
          plan_change_payments=EXCLUDED.plan_change_payments,unclassified_payments=EXCLUDED.unclassified_payments,
          finalized_at=NULL,updated_at=NOW()'''),
          {**counts,'month':month,'code':code,'tz':REPORT_TIMEZONE,'summary':json.dumps(summary)})
    return len(reports)


def track_subscription(platform, external_id):
    with SessionLocal() as db:
        freeze_attribution(db, platform, external_id)
        snapshot = load_snapshot(db, platform, external_id)
        snapshot = dict(snapshot) if snapshot else None
        db.commit()
    if not snapshot or not snapshot['referral_code'] or not snapshot['since'] or not snapshot['user_id']:
        return 0
    events, linked = (collect_apple if platform == 'ios' else collect_google)(external_id, snapshot['since'])
    with SessionLocal() as db:
        count = record_events(db, snapshot, events, linked)
        if count:
            refresh_monthly_reports(db)
        db.commit()
    return count


def track_subscription_best_effort(platform, external_id):
    try:
        return track_subscription(platform, external_id)
    except Exception as exc:
        # Do not log tokens, receipts, customer emails or store URLs.
        logger.warning('Referral billing collection failed: platform=%s error_type=%s; daily sync will retry', platform, type(exc).__name__)
        return None


def run_referral_billing_sync():
    with SessionLocal() as db:
        freeze_attribution(db)
        rows = db.execute(text('''SELECT platform,external_id FROM subscription_entitlements
          WHERE referral_code IS NOT NULL AND last_seen_user_id IS NOT NULL ORDER BY last_verified_at NULLS FIRST''')).mappings().all()
        db.commit()
    result = {'checked': len(rows), 'failed': 0, 'recorded': 0}
    for row in rows:
        count = track_subscription_best_effort(row['platform'], row['external_id'])
        result['failed' if count is None else 'recorded'] += 1 if count is None else count
    # Also repair derived reports after a previous report-write outage.
    with SessionLocal() as db:
        refresh_monthly_reports(db)
        db.commit()
    return result
