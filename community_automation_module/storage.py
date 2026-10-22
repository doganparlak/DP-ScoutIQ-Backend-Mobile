"""Separate automation storage and read-only combined leaderboard sources."""
from sqlalchemy import text


def migrate(db):
    db.execute(text('SELECT pg_advisory_xact_lock(81423020)'))
    db.execute(text('CREATE SEQUENCE IF NOT EXISTS community_automation_account_ids INCREMENT BY -1 START WITH -1 MAXVALUE -1'))
    db.execute(text('''CREATE TABLE IF NOT EXISTS community_automation_accounts (
        user_id bigint PRIMARY KEY DEFAULT nextval('community_automation_account_ids'),
        nickname text NOT NULL UNIQUE,
        tier text NOT NULL CHECK(tier IN ('pro','plus','free')),
        created_at timestamptz NOT NULL DEFAULT NOW())'''))
    db.execute(text('ALTER TABLE community_automation_accounts ADD COLUMN IF NOT EXISTS nickname text'))
    db.execute(text("ALTER TABLE community_automation_accounts ALTER COLUMN user_id SET DEFAULT nextval('community_automation_account_ids')"))
    # Freeze the affected entry tables while copying; external fixture fetching
    # need not finish before this migration can run.
    db.execute(text('LOCK TABLE prediction_entries,daily_scout_attempts,community_automation_accounts IN SHARE ROW EXCLUSIVE MODE'))
    # Only the explicitly registered legacy automation users may be migrated.
    legacy = db.execute(text('''SELECT u.id,u.community_nickname,u.email FROM users u
        JOIN community_automation_accounts a ON a.user_id=u.id''')).mappings().all()
    if any(not row['email'].endswith('@community-automation.invalid') for row in legacy):
        raise ValueError('Registry references a real account; migration aborted')
    db.execute(text('''UPDATE community_automation_accounts a SET nickname=u.community_nickname
        FROM users u WHERE u.id=a.user_id AND a.nickname IS NULL'''))
    db.execute(text('ALTER TABLE community_automation_accounts ALTER COLUMN nickname SET NOT NULL'))
    db.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS community_automation_nickname_unique ON community_automation_accounts(lower(nickname))'))
    for source, target in [('prediction_entries','community_automation_prediction_entries'),
                           ('daily_scout_attempts','community_automation_discovery_attempts')]:
        db.execute(text(f'CREATE TABLE IF NOT EXISTS {target} (LIKE {source} INCLUDING ALL)'))
        db.execute(text(f'''INSERT INTO {target} SELECT e.* FROM {source} e
            JOIN community_automation_accounts a ON a.user_id=e.user_id ON CONFLICT DO NOTHING'''))
        # Verify copies before removing the original rows (not merely row counts).
        missing = db.execute(text(f'''SELECT EXISTS(SELECT e.* FROM {source} e
            JOIN community_automation_accounts a ON a.user_id=e.user_id
            EXCEPT SELECT * FROM {target})''')).scalar_one()
        if missing:
            raise ValueError('Automation entry copy did not match the original')
        db.execute(text(f'DELETE FROM {source} WHERE user_id IN (SELECT user_id FROM community_automation_accounts)'))
    constraints = db.execute(text('''SELECT conname FROM pg_constraint
        WHERE conrelid='community_automation_accounts'::regclass AND contype='f'
        AND confrelid='users'::regclass''')).scalars().all()
    for name in constraints:
        quoted = db.get_bind().dialect.identifier_preparer.quote(name)
        db.execute(text(f'ALTER TABLE community_automation_accounts DROP CONSTRAINT {quoted}'))
    if legacy:
        # Normal accounts have never been referenced by this operation.
        deleted = db.execute(text('DELETE FROM users WHERE id=ANY(:ids)'),
                             {'ids': [r['id'] for r in legacy]}).rowcount
        if deleted != len(legacy):
            raise ValueError('Unexpected legacy account removal count')
    db.execute(text('''CREATE OR REPLACE VIEW community_league_profiles WITH (security_invoker=true) AS
        SELECT id,community_nickname,plan,subscription_end_at FROM users
        UNION ALL
        SELECT user_id,nickname,
          CASE tier WHEN 'pro' THEN 'Pro Monthly' WHEN 'plus' THEN 'No Ads Monthly' ELSE 'Free' END,
          CASE WHEN tier='free' THEN NULL::timestamptz ELSE '2100-01-01'::timestamptz END
        FROM community_automation_accounts'''))
    db.execute(text('''CREATE OR REPLACE VIEW community_league_prediction_entries WITH (security_invoker=true) AS
        SELECT * FROM prediction_entries UNION ALL SELECT * FROM community_automation_prediction_entries'''))
    db.execute(text('''CREATE OR REPLACE VIEW community_league_discovery_attempts WITH (security_invoker=true) AS
        SELECT * FROM daily_scout_attempts UNION ALL SELECT * FROM community_automation_discovery_attempts'''))
    # These are backend storage/read sources, not new Supabase client APIs.
    relations = ('community_automation_accounts', 'community_automation_prediction_entries',
                 'community_automation_discovery_attempts', 'community_league_profiles',
                 'community_league_prediction_entries', 'community_league_discovery_attempts')
    for role in ('PUBLIC', 'anon', 'authenticated'):
        if role != 'PUBLIC' and not db.execute(text('SELECT 1 FROM pg_roles WHERE rolname=:role'), {'role': role}).scalar():
            continue
        db.execute(text('REVOKE ALL ON ' + ','.join(relations) + ' FROM ' + role))
