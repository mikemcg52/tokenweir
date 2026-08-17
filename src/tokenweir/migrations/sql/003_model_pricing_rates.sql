-- 003 — the rate card.
--
-- Prices live here and nowhere else. `gateway_usage` stores raw counts; a dollar
-- figure is produced by joining these rates at report time (005). That is what
-- makes repricing a model a matter of inserting a row rather than rewriting
-- history, and what makes it possible to say "I do not know what this cost"
-- instead of guessing.
--
-- **Effective-dated, not current-valued.** The primary key is (model,
-- effective_from) and the rollup uses the entry in force on the usage day, so
-- publishing a new price does not silently restate last month's bill.
--
-- **The unit is in the column name on purpose.** Vendors publish per-million-token
-- prices; some rate cards are written per-thousand. A rate card loaded against
-- the wrong unit is a thousand-fold error in a dollar figure with nothing in the
-- data to reveal it, so the unit is spelled out where a loader's author cannot
-- miss it rather than left to documentation.

CREATE TABLE IF NOT EXISTS model_pricing_rates (
    -- Opaque, provider-neutral model identifier -- the same string the record
    -- carries, stored verbatim. A local Ollama model is priced exactly like a
    -- Claude one (it may well be priced at zero); nothing here knows about
    -- providers.
    model                        TEXT           NOT NULL,

    -- The first day this rate applies. Inclusive; the rate stays in force until
    -- a later row supersedes it.
    effective_from               DATE           NOT NULL,

    input_cost_usd_per_mtok      NUMERIC(18, 10) NOT NULL,
    output_cost_usd_per_mtok     NUMERIC(18, 10) NOT NULL,

    -- Nullable, and their nullness is meaningful: a rate card that does not price
    -- cache traffic cannot price a call that used it. The rollup treats such a
    -- call as unpriced rather than counting the cache tokens as free, which would
    -- understate the bill silently.
    cache_write_cost_usd_per_mtok NUMERIC(18, 10),
    cache_read_cost_usd_per_mtok  NUMERIC(18, 10),

    -- Provenance, so an operator can tell where a number came from when it is
    -- disputed against an invoice.
    source                       TEXT,
    loaded_at                    TIMESTAMPTZ    NOT NULL DEFAULT now(),

    PRIMARY KEY (model, effective_from),

    CONSTRAINT model_pricing_rates_costs_non_negative CHECK (
        input_cost_usd_per_mtok >= 0
        AND output_cost_usd_per_mtok >= 0
        AND (cache_write_cost_usd_per_mtok IS NULL OR cache_write_cost_usd_per_mtok >= 0)
        AND (cache_read_cost_usd_per_mtok IS NULL OR cache_read_cost_usd_per_mtok >= 0)
    )
);
