-- 002 — parent_request_id.
--
-- A fan-out endpoint (the gateway's /compare) issues several model calls for one
-- client request. Each sub-call is its own metered unit of work and gets its own
-- row; parent_request_id is what lets a reader roll them back up to the request
-- the user actually made, and what lets a cost report avoid double-counting a
-- fan-out as N independent requests.
--
-- Nullable, because the ordinary case is a call with no parent.

ALTER TABLE gateway_usage
    ADD COLUMN IF NOT EXISTS parent_request_id TEXT;

-- Partial: the column is null for most rows, and the only query that uses it
-- ("give me the children of this request") never asks for the nulls. Indexing
-- them would cost write throughput on the common path to serve nothing.
CREATE INDEX IF NOT EXISTS gateway_usage_parent_request_id_idx
    ON gateway_usage (parent_request_id)
    WHERE parent_request_id IS NOT NULL;
