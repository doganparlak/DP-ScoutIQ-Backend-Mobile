"""Resolve one account's access across all of its store subscriptions."""
import datetime as dt
from sqlalchemy import text
from api_module.utilities import plan_from_product_id


def plan_priority(plan):
    return 2 if plan in ('Pro Monthly', 'Pro Yearly') else 1 if plan == 'No Ads Monthly' else 0


def best_entitlement(entitlements, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    active = [e for e in entitlements if e.get('is_active') and e.get('expires_at') and e['expires_at'] > now]
    return max(active, key=lambda e: (plan_priority(plan_from_product_id(e['product_id'])), e['expires_at']), default=None)


def reconcile_subscription(db, user_id, *, allow_email_restore=False):
    """Caller commits. Never move purchases owned by another ScoutWise user."""
    user = db.execute(text('SELECT * FROM users WHERE id=:uid FOR UPDATE'), {'uid': user_id}).mappings().first()
    if not user:
        return None
    now = dt.datetime.now(dt.timezone.utc)
    expiry = user.get('subscription_end_at')
    if (user.get('subscription_platform') == 'manual'
            and not user.get('subscription_external_id')
            and plan_priority(user.get('plan')) > 0
            and expiry and expiry > now):
        # An administrator grant is not a missing store purchase. Preserve its
        # plan and expiry on login, /me refresh, and background reconciliation.
        return user
    entitlements = list(db.execute(text('''SELECT * FROM subscription_entitlements
        WHERE last_seen_user_id=:uid OR
          (:restore AND last_seen_user_id IS NULL AND lower(last_seen_email)=lower(:email))
        FOR UPDATE'''), {'uid': user_id, 'restore': allow_email_restore, 'email': user['email']}).mappings().all())
    for e in entitlements:
        if e['last_seen_user_id'] is None and e.get('is_active') and e.get('expires_at') and e['expires_at'] > now:
            db.execute(text('''UPDATE subscription_entitlements SET last_seen_user_id=:uid,updated_at=NOW()
                WHERE platform=:platform AND external_id=:external AND last_seen_user_id IS NULL'''),
                {'uid': user_id, 'platform': e['platform'], 'external': e['external_id']})
    # Preserve older store purchases until migrated to the entitlement ledger.
    tracked_current = any(e['platform'] == user.get('subscription_platform') and
                          e['external_id'] == user.get('subscription_external_id') for e in entitlements)
    if user.get('subscription_external_id') and not tracked_current:
        entitlements.append({'platform': user['subscription_platform'], 'external_id': user['subscription_external_id'],
            'product_id': {'Pro Monthly': 'pro_monthly', 'Pro Yearly': 'pro_yearly', 'No Ads Monthly': 'no_ads_monthly'}.get(user['plan'], ''),
            'is_active': plan_priority(user['plan']) > 0, 'expires_at': user.get('subscription_end_at'),
            'auto_renew': user.get('subscription_auto_renew')})
    winner = best_entitlement(entitlements, now)
    if winner:
        values = {'plan': plan_from_product_id(winner['product_id']), 'expiry': winner['expires_at'],
                  'renew': bool(winner['auto_renew']), 'platform': winner['platform'], 'external': winner['external_id']}
    elif (not entitlements and not user.get('subscription_external_id')
          and user.get('subscription_platform') in (None, 'manual')
          and (expiry is None or expiry > now)):
        # Also preserve legacy manual grants without a platform marker. A dated
        # grant must still be active; expired grants fall through to Free.
        return user
    else:
        values = {'plan': 'Free', 'expiry': None, 'renew': False, 'platform': None, 'external': None}
    fields = {'plan': 'plan', 'expiry': 'subscription_end_at', 'renew': 'subscription_auto_renew',
              'platform': 'subscription_platform', 'external': 'subscription_external_id'}
    if all(user.get(column) == values[key] for key, column in fields.items()):
        return user
    return db.execute(text('''UPDATE users SET plan=:plan,subscription_end_at=:expiry,
        subscription_auto_renew=:renew,subscription_platform=:platform,subscription_external_id=:external,
        subscription_receipt=CASE WHEN subscription_platform=:platform AND subscription_external_id=:external
            THEN subscription_receipt ELSE NULL END
        WHERE id=:uid RETURNING *'''), {**values, 'uid': user_id}).mappings().one()
