-- Daily-match tracking is separate from the match report portfolio.
CREATE TABLE IF NOT EXISTS public.match_watchlist (
    user_id bigint NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    fixture_id bigint NOT NULL CHECK (fixture_id > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, fixture_id)
);
ALTER TABLE public.match_watchlist ENABLE ROW LEVEL SECURITY;
-- Mobile uses its own sessions table, not Supabase Auth JWTs. Only the
-- authenticated backend accesses this table; no direct client grants/policies.
REVOKE ALL ON public.match_watchlist FROM PUBLIC, anon, authenticated;
