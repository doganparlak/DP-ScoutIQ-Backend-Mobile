# Database-side scouting percentiles

Apply `20261112_report_percentiles.sql` to Supabase before deploying the matching
backend code. The enterprise and mobile repositories carry the same migration;
apply it only once to their shared database. It is safe to reapply.

The backend loads only the selected player's domestic-league stints. PostgreSQL
normalizes and ranks the comparison players, returning the existing response
shape, role groups, metric thresholds, tied midranks, and league/team/PDF scopes.
Team-analysis season percentiles use a separate path and are unchanged.

`scoutwise_report_peer_cache_v1` contains only derived, rounded peer values.
A statement-level trigger advances a small revision counter on source INSERT,
UPDATE, DELETE, or TRUNCATE. Unchanged requests read that counter only; they do
not scan or hash league source data. After a source revision, the next request
refreshes each affected/requested league once, reading one player's stints at a
time and reusing unchanged profiles by source hash. Sorted per-metric arrays
and cohort counts are then rebuilt inside Supabase. A per-league transaction
lock prevents duplicate rebuilds; warm requests do not acquire that lock.
Data Module needs no extra step and no historical imports are started.
Changes to normalization require a new cache algorithm version in the migration.

Cache updates use their own short database transaction and never commit the
caller's report transaction. A separate pool caps benchmark transactions at
four connections, preventing callers from exhausting the main request pool
while waiting for another connection. The cache tables have RLS enabled, and only the
backend database owner and service role can access its functions/data. There
is no client RPC access and no fallback that loads all league peers into RAM.

First use of a league, or a source refresh, may take longer to populate the
compact database cache. Subsequent requests reuse unchanged profiles. Binary
searches of sorted metric arrays preserve tied midranks and replace the selected
player with their requested team stint exactly once. Identical scopes share
results within the same request. Work has moved from backend RAM to database CPU/storage; keep monitoring both services.
The migration changes no player source records, report text, or embeddings.
