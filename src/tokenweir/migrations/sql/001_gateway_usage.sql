-- 001 — the usage log.
--
-- One row per metered unit of work (one model call / iteration). The column set
-- is the v1 usage-record contract's field set: `tokenweir.contract.UsageRecord`
-- is the definition of what a record is, and a test asserts the two agree, so a
-- field added to the record without a migration fails the suite rather than
-- being silently dropped on write.
--
-- Raw counts only. There is deliberately **no cost column** — cost is derived at
-- report time from these counts and the effective-dated rate card (003), because
-- a stored dollar goes stale the moment a rate card changes and nothing in the
-- row then says which figures are stale. This is enforced by a test, not left as
-- a convention.
--
-- Forward-only: this file is immutable once released. A change is 007.

CREATE TABLE IF NOT EXISTS gateway_usage (
    id                          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- The contract version that wrote the row. Stored per row rather than
    -- assumed, so that after a v1 -> v2 contract change a reader can tell which
    -- rows predate it; the contract's own compatibility rules anticipate exactly
    -- that situation.
    schema_version              INTEGER     NOT NULL,

    -- Identity. Non-blank on the record by contract; NOT NULL here. No unique
    -- constraint on request_id: this is an append-only log, /compare emits
    -- several records sharing a parent, and an at-least-once broker is expected
    -- to redeliver. A unique index would turn an ordinary redelivery into a
    -- poison message.
    request_id                  TEXT        NOT NULL,
    app_id                      TEXT        NOT NULL,
    endpoint                    TEXT        NOT NULL,
    model                       TEXT        NOT NULL,
    status                      TEXT        NOT NULL,

    -- Attribution. Optional on the record, so nullable here. Not blank-checked,
    -- matching the contract: '' is legal for these where it is not for identity.
    workload                    TEXT,
    queue                       TEXT,

    -- Raw token counts. BIGINT rather than INTEGER because the contract's only
    -- bound on these is "non-negative integer" -- the column matches the contract
    -- rather than a guess about plausible magnitudes. The guess would be wrong in
    -- the one place it matters: a producer that sums a turn's messages (the
    -- subscription Stop hook) or a long-running batch is exactly where a count
    -- creeps past 2^31, and an overflow there fails the whole batch it is in.
    input_tokens                BIGINT      NOT NULL DEFAULT 0,
    output_tokens               BIGINT      NOT NULL DEFAULT 0,
    cache_creation_input_tokens BIGINT      NOT NULL DEFAULT 0,
    cache_read_input_tokens     BIGINT      NOT NULL DEFAULT 0,

    -- Operational. BIGINT for the same reason as the counts.
    latency_ms                  BIGINT,

    -- Whether a rate card applies at all (ADR-0001 Pillar 4). Left as TEXT
    -- rather than an enum type: the contract may add a mode, and adding a value
    -- to a Postgres enum is a schema change in every consumer's database, while
    -- the contract already validates the value on the way in.
    pricing_mode                TEXT,

    -- The instant metered. NOT NULL with a server-side default so that a record
    -- carrying no timestamp is stamped by the database rather than by whichever
    -- client happened to write it -- clients disagree, one server does not.
    ts                          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Time-ordered reads (the "what happened recently" query).
CREATE INDEX IF NOT EXISTS gateway_usage_ts_idx
    ON gateway_usage (ts);

-- Per-app time ranges. The *day-bucketed* index a rollup query needs is 006 and
-- must use an IMMUTABLE expression; this one indexes the raw column and is a
-- different access path, not a duplicate of it.
CREATE INDEX IF NOT EXISTS gateway_usage_app_ts_idx
    ON gateway_usage (app_id, ts);
