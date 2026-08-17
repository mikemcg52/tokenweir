-- 005 — the daily rollup, and the only place a dollar figure is produced.
--
-- Three properties here are load-bearing and each is pinned by a test that reads
-- this file, because each of them was a bug once.
--
-- 1. THE DAY BUCKET IS IMMUTABLE, NOT `DATE_TRUNC`.
--    `DATE_TRUNC('day', ts)::DATE` over a TIMESTAMPTZ is only STABLE -- its
--    result depends on the session TimeZone -- so Postgres will not build an
--    index on it. Without an index a thirty-day query sequential-scans the whole
--    log, which past a million rows misses the 2-second reporting SLA.
--    `(ts AT TIME ZONE INTERVAL '0')::DATE` is IMMUTABLE, indexable, and pins the
--    day to UTC so two producers in different zones agree on which day a call
--    belongs to. 006 indexes this same expression; the two must stay identical or
--    the index cannot serve the view.
--
-- 2. `is_priced` IS A `BOOL_AND`, NOT A `BOOL_OR`.
--    If any call in a group has no applicable rate, the group's cost is unknown --
--    not "the part I could price". A partial sum is indistinguishable from a
--    complete one when you read it, and it is always too low. So the flag is true
--    only when every call priced, and `est_cost_usd` is NULL when it is not.
--    An operator chasing a NULL looks for the missing rate-card entry; an
--    operator reading an understated number does not know to look at all.
--
-- 3. SUBSCRIPTION USAGE IS NEVER PRICED.
--    Under a flat-rate Claude Max subscription there is no per-call dollar
--    (ADR-0001 Pillar 4). Multiplying those tokens by an API rate card would
--    manufacture a number that does not correspond to anything anyone is billed,
--    which is worse than a blank. `pricing_mode` is in the GROUP BY so that one
--    subscription record cannot blank out the cost of API-metered usage that
--    genuinely has one -- the two are reported side by side instead.
--
-- Note for TOKWEIR-10: the `pricing_mode` grouping column is a deliberate
-- divergence from the gateway's view and changes its shape. The gateway's
-- operator queries must be revisited when it adopts this.

CREATE OR REPLACE VIEW gateway_usage_daily AS
SELECT
    u.app_id,
    (u.ts AT TIME ZONE INTERVAL '0')::DATE                    AS usage_day,
    u.model,
    u.pricing_mode,

    COUNT(*)                                                  AS calls,
    SUM(u.input_tokens)::BIGINT                               AS input_tokens,
    SUM(u.output_tokens)::BIGINT                              AS output_tokens,
    SUM(u.cache_creation_input_tokens)::BIGINT                AS cache_creation_input_tokens,
    SUM(u.cache_read_input_tokens)::BIGINT                    AS cache_read_input_tokens,

    -- A call is priced only if a rate was in force for its model that day, it is
    -- not flat-rate subscription usage, and every token class it actually used
    -- has a rate. That last clause is why a rate card without cache prices makes
    -- a cache-using call unpriced rather than free: counting unpriced cache
    -- tokens at zero would understate the bill and look exactly like a call that
    -- used no cache.
    BOOL_AND(
        rate.model IS NOT NULL
        AND u.pricing_mode IS DISTINCT FROM 'subscription'
        AND (u.cache_creation_input_tokens = 0 OR rate.cache_write_cost_usd_per_mtok IS NOT NULL)
        AND (u.cache_read_input_tokens = 0 OR rate.cache_read_cost_usd_per_mtok IS NOT NULL)
    )                                                         AS is_priced,

    CASE
        WHEN BOOL_AND(
            rate.model IS NOT NULL
            AND u.pricing_mode IS DISTINCT FROM 'subscription'
            AND (u.cache_creation_input_tokens = 0 OR rate.cache_write_cost_usd_per_mtok IS NOT NULL)
            AND (u.cache_read_input_tokens = 0 OR rate.cache_read_cost_usd_per_mtok IS NOT NULL)
        )
        THEN SUM(
            (
                  u.input_tokens                * rate.input_cost_usd_per_mtok
                + u.output_tokens               * rate.output_cost_usd_per_mtok
                + u.cache_creation_input_tokens * COALESCE(rate.cache_write_cost_usd_per_mtok, 0)
                + u.cache_read_input_tokens     * COALESCE(rate.cache_read_cost_usd_per_mtok, 0)
            ) / 1000000
        )
        ELSE NULL
    END                                                       AS est_cost_usd

FROM gateway_usage u

-- The rate in force on the usage day: the latest entry whose effective_from is
-- not after that day. LATERAL rather than a correlated subquery per column so
-- the lookup happens once per row, and LEFT so an unpriced model still appears
-- in the rollup -- disappearing would hide the usage as well as the cost.
LEFT JOIN LATERAL (
    SELECT
        r.model,
        r.input_cost_usd_per_mtok,
        r.output_cost_usd_per_mtok,
        r.cache_write_cost_usd_per_mtok,
        r.cache_read_cost_usd_per_mtok
    FROM model_pricing_rates r
    WHERE r.model = u.model
      AND r.effective_from <= (u.ts AT TIME ZONE INTERVAL '0')::DATE
    ORDER BY r.effective_from DESC
    LIMIT 1
) rate ON TRUE

GROUP BY
    u.app_id,
    (u.ts AT TIME ZONE INTERVAL '0')::DATE,
    u.model,
    u.pricing_mode;

-- The view is a new relation, so it needs its own grant -- a grant on the
-- underlying table does not reach it. Same role-resolution rules as 004.
DO $$
DECLARE
    reader_role TEXT := NULLIF(current_setting('tokenweir.reader_role', true), '');
BEGIN
    IF reader_role IS NULL THEN
        RAISE NOTICE
            'tokenweir: no reader role configured (tokenweir.reader_role is unset); '
            'skipping SELECT grant on gateway_usage_daily';
        RETURN;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = reader_role) THEN
        RAISE NOTICE
            'tokenweir: reader role % does not exist; skipping SELECT grant on '
            'gateway_usage_daily', reader_role;
        RETURN;
    END IF;

    EXECUTE format('GRANT SELECT ON gateway_usage_daily TO %I', reader_role);
END
$$;
