-- 006 — the functional index the rollup actually needs.
--
-- 001's (app_id, ts) index does not serve a query that groups by *day*: the
-- planner cannot match a raw-column index to an expression in GROUP BY. This
-- indexes the expression itself.
--
-- The expression must be character-for-character the one in 005 (modulo the
-- table qualifier the view needs and an index does not), or Postgres treats them
-- as different expressions and the index is never chosen. A test compares the
-- two rather than trusting that a later edit to one will be mirrored in the
-- other -- an index silently not being used is invisible until the table is
-- large, which is exactly when it matters.
--
-- Why not `DATE_TRUNC('day', ts)::DATE`: over a TIMESTAMPTZ that expression is
-- STABLE, not IMMUTABLE, because its result depends on the session TimeZone --
-- so this CREATE INDEX would simply fail. That failure is the origin of the fix
-- being preserved here.
--
-- Plain CREATE INDEX rather than CONCURRENTLY: the runner applies each migration
-- inside a transaction (so a failure cannot leave a version recorded as applied
-- without its objects), and CONCURRENTLY cannot run in one. Building this index
-- takes a write lock on gateway_usage for its duration.

CREATE INDEX IF NOT EXISTS gateway_usage_app_day_idx
    ON gateway_usage (app_id, ((ts AT TIME ZONE INTERVAL '0')::DATE));
