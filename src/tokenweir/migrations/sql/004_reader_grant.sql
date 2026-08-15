-- 004 — read access for a reporting role.
--
-- The writing role and the reading role are separate: a dashboard query has no
-- business holding INSERT on the usage log. Postgres grants no privileges to
-- other roles by default, so every relation this package creates needs an
-- explicit grant migration; this is the pattern the later ones follow.
--
-- **The role name is configuration, not a constant.** `tokenweir` is a library
-- that has to install into a homelab, an EKS deployment and a customer tenant,
-- and those do not share role names -- a hardcoded GRANT to one of them would
-- make the migration set fail everywhere else. The runner sets
-- `tokenweir.reader_role` (SET LOCAL, inside the migration's own transaction)
-- when a role is configured; the block below no-ops with a notice when it is not
-- set, is blank, or names a role the server does not have.
--
-- Silence would be the wrong behaviour in either direction here: failing would
-- make an unconfigured deployment un-migratable, and saying nothing would let an
-- operator believe a grant happened that did not. The notice is the middle.

DO $$
DECLARE
    reader_role TEXT := NULLIF(current_setting('tokenweir.reader_role', true), '');
BEGIN
    IF reader_role IS NULL THEN
        RAISE NOTICE
            'tokenweir: no reader role configured (tokenweir.reader_role is unset); '
            'skipping SELECT grants on gateway_usage and model_pricing_rates';
        RETURN;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = reader_role) THEN
        RAISE NOTICE
            'tokenweir: reader role % does not exist; skipping SELECT grants on '
            'gateway_usage and model_pricing_rates', reader_role;
        RETURN;
    END IF;

    -- format(%I) quotes the identifier, so a role name needing quoting works and
    -- a hostile one cannot inject. The role has already been proven to exist by
    -- the lookup above, so this is not the only thing standing between the
    -- server and an arbitrary string.
    EXECUTE format('GRANT SELECT ON gateway_usage TO %I', reader_role);
    EXECUTE format('GRANT SELECT ON model_pricing_rates TO %I', reader_role);
END
$$;
