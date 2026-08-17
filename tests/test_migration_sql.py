"""The preserved fixes, asserted against the shipped SQL (TOKWEIR-5).

TOKWEIR-5 says "keep the `DATE_TRUNC` STABLE/IMMUTABLE and `is_priced`/`BOOL_AND`
fixes", and "forward-only, no DROP-without-review preserved". A fix kept only as a
comment is a fix waiting to be reverted, so each of them is a test here.

These read the SQL text and **need no database**, so they hold on a bare
`pip install -e .` where the real-Postgres suite skips.

They are a second line, not the only one, and the difference matters. An earlier
draft of this suite claimed no Postgres could be had here; it could (`pgserver`,
see `tests/conftest.py`), and `test_postgres_integration.py` now runs. Reading the
SQL proves a fix is still *written*, never that it *works*: `BOOL_AND` was
"covered" by a grep in this file while every behavioural test passed with
`BOOL_OR` substituted. Add the behavioural test in the integration suite; keep
these for the environment that has no server.

Every check strips comments first. Without that, the prose above each migration —
which necessarily names `DATE_TRUNC` in order to explain why it is not used — would
be read as the thing it warns against, and the tests would fail on their own
documentation.
"""

import re
import subprocess
import sys
import tomllib
import zipfile
from dataclasses import fields
from fnmatch import fnmatch
from importlib import resources
from pathlib import Path

import pytest

from tokenweir.contract import UsageRecord
from tokenweir.migrations import (
    SCHEMA_MIGRATIONS_TABLE,
    destructive_statements,
    discover,
)
from tokenweir.postgres import COLUMNS as INSERT_COLUMNS

#: Same shapes the runner strips. Duplicated deliberately rather than imported:
#: if the runner's stripping ever broke, importing it would break these checks in
#: the same direction and they would agree with each other while both being wrong.
_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)

#: The IMMUTABLE day bucket, in either the qualified form a view needs
#: (`u.ts`) or the bare form an index takes.
_DAY_EXPRESSION_RE = re.compile(
    r"\(\s*(?:(?P<qualifier>\w+)\.)?ts\s+AT\s+TIME\s+ZONE\s+INTERVAL\s+'0'\s*\)\s*::\s*DATE",
    re.IGNORECASE,
)

#: The canonical text, after the table qualifier is removed and whitespace
#: normalized. An index cannot carry the view's alias, so "character-for-character
#: identical" can only mean identical modulo that qualifier — and the comparison
#: is done by normalizing rather than by eye.
_CANONICAL_DAY_EXPRESSION = "(ts AT TIME ZONE INTERVAL '0')::DATE"


def _strip_comments(sql: str) -> str:
    return _COMMENT_RE.sub(" ", sql)


def _normalize_day_expression(match: re.Match) -> str:
    text = match.group(0)
    qualifier = match.group("qualifier")
    if qualifier:
        text = text.replace(f"{qualifier}.ts", "ts", 1)
    return re.sub(r"\s+", " ", text).strip()


@pytest.fixture(scope="module")
def migrations():
    return discover()


@pytest.fixture(scope="module")
def code_by_filename(migrations):
    """Every migration's SQL with comments removed, keyed by filename."""
    return {m.filename: _strip_comments(m.sql) for m in migrations}


# --- The migration set itself (FR-006) ----------------------------------------


def test_the_six_migrations_ship(migrations):
    assert [m.filename for m in migrations] == [
        "001_gateway_usage.sql",
        "002_parent_request_id.sql",
        "003_model_pricing_rates.sql",
        "004_reader_grant.sql",
        "005_gateway_usage_daily.sql",
        "006_gateway_usage_app_day_index.sql",
    ]


def test_versions_are_contiguous_zero_padded_and_unique(migrations):
    """Lexical order is only numeric order if the padding holds.

    `discover()` enforces this and raises, so this test pins the *property* rather
    than the error path — a seventh migration named `7_thing.sql` should fail here
    with a readable message, not somewhere downstream at apply time.
    """
    versions = [m.version for m in migrations]
    assert versions == list(range(1, len(migrations) + 1))
    assert len(set(versions)) == len(versions)
    assert [m.filename for m in migrations] == sorted(m.filename for m in migrations)


# --- Fix 1: the day bucket is IMMUTABLE (FR-024) ------------------------------


def test_date_trunc_is_not_used_for_day_bucketing(code_by_filename):
    """`DATE_TRUNC('day', ts)::DATE` over a TIMESTAMPTZ is STABLE, not IMMUTABLE.

    Postgres will not index a STABLE expression, so the rollup's 30-day query
    sequential-scans the whole log and misses the 2-second SLA past a million
    rows. The fix is `(ts AT TIME ZONE INTERVAL '0')::DATE`.
    """
    offenders = {
        name: code for name, code in code_by_filename.items() if "DATE_TRUNC" in code.upper()
    }
    assert offenders == {}, (
        "DATE_TRUNC appears in "
        + ", ".join(sorted(offenders))
        + " — over a TIMESTAMPTZ it is STABLE, not IMMUTABLE, so the functional "
        "index in 006 cannot be built on it and the rollup loses its index"
    )


def test_the_rollup_and_the_index_bucket_the_day_the_same_way(code_by_filename):
    """Two different expressions mean the planner never chooses the index.

    Compared by extracting both from the SQL and normalizing the table qualifier —
    an index cannot carry the view's alias — rather than by trusting that a later
    edit to one gets mirrored in the other. An unused index is invisible until the
    table is large, which is exactly when it is needed.
    """
    view = code_by_filename["005_gateway_usage_daily.sql"]
    index = code_by_filename["006_gateway_usage_app_day_index.sql"]

    view_expressions = {
        _normalize_day_expression(m) for m in _DAY_EXPRESSION_RE.finditer(view)
    }
    index_expressions = {
        _normalize_day_expression(m) for m in _DAY_EXPRESSION_RE.finditer(index)
    }

    assert view_expressions, "the rollup does not bucket by day at all"
    assert index_expressions, "006 does not index a day expression at all"
    assert view_expressions == index_expressions == {_CANONICAL_DAY_EXPRESSION}


def test_the_view_buckets_and_groups_by_the_same_expression(code_by_filename):
    """SELECT and GROUP BY must agree, or Postgres rejects the view outright.

    Cheap to assert here and it fails at test time rather than at apply time on
    somebody's deploy.
    """
    view = code_by_filename["005_gateway_usage_daily.sql"]
    occurrences = [_normalize_day_expression(m) for m in _DAY_EXPRESSION_RE.finditer(view)]
    assert len(occurrences) >= 2, (
        "expected the day expression in both the select list and the GROUP BY"
    )
    assert set(occurrences) == {_CANONICAL_DAY_EXPRESSION}


# --- Fix 2: is_priced is a BOOL_AND (FR-025, FR-026) --------------------------


def test_the_priced_flag_is_bool_and_not_bool_or(code_by_filename):
    """If any call in a group is unpriced, the group's cost is unknown.

    `BOOL_OR` would report a number that is always too low and is
    indistinguishable, when read, from a complete one. An operator chasing a NULL
    goes looking for the missing rate-card entry; an operator reading an
    understated figure does not know to look.
    """
    view = code_by_filename["005_gateway_usage_daily.sql"]
    assert "BOOL_AND" in view.upper()
    assert "BOOL_OR" not in view.upper()
    assert re.search(r"\bAS\s+is_priced\b", view, re.IGNORECASE)


def test_an_unpriced_group_yields_a_null_cost(code_by_filename):
    """The flag is only half of it — the cost has to actually be blanked."""
    view = code_by_filename["005_gateway_usage_daily.sql"]
    assert re.search(
        r"CASE\s+WHEN\s+BOOL_AND\b.*?ELSE\s+NULL\s+END\s+AS\s+est_cost_usd",
        view,
        re.IGNORECASE | re.DOTALL,
    ), "est_cost_usd is not NULL-ed for a group that is not fully priced"


def test_subscription_usage_is_never_priced(code_by_filename):
    """Under a flat-rate Max subscription there is no per-call dollar.

    Multiplying those tokens by an API rate card manufactures a figure nobody is
    billed — worse than a blank, because it looks like an answer (ADR-0001
    Pillar 4).
    """
    view = code_by_filename["005_gateway_usage_daily.sql"]
    assert re.search(
        r"pricing_mode\s+IS\s+DISTINCT\s+FROM\s+'subscription'", view, re.IGNORECASE
    ), "the priced flag does not exclude subscription usage"
    assert re.search(r"GROUP\s+BY(.|\n)*?pricing_mode", view, re.IGNORECASE), (
        "pricing_mode is not in the GROUP BY — without it one subscription record "
        "blanks out the cost of API-metered usage that genuinely has one"
    )


def test_the_rate_in_force_is_used_not_the_newest(code_by_filename):
    """Effective-dating: repricing a model must not restate last month."""
    view = code_by_filename["005_gateway_usage_daily.sql"]
    assert re.search(r"effective_from\s*<=", view, re.IGNORECASE)
    assert re.search(r"ORDER\s+BY\s+r\.effective_from\s+DESC", view, re.IGNORECASE)
    assert re.search(r"\bLIMIT\s+1\b", view, re.IGNORECASE)


# --- "No DROP without operator review" (FR-010) -------------------------------


def test_no_shipped_migration_is_destructive(migrations):
    offenders = {
        m.filename: found for m in migrations if (found := destructive_statements(m.sql))
    }
    assert offenders == {}, f"destructive statement(s) in shipped migrations: {offenders}"


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE gateway_usage;",
        "drop index gateway_usage_ts_idx;",
        "TRUNCATE gateway_usage;",
        "DELETE FROM gateway_usage WHERE ts < now();",
        "ALTER TABLE gateway_usage DROP COLUMN queue;",
        # A `--` inside a string literal is not a comment. Stripping comments by
        # regex read this one as "comment from `--` to end of line" and hid the
        # DROP sharing that line — the guard's one false *negative*, and the
        # dangerous direction for a guard whose whole job is to refuse.
        "INSERT INTO audit(reason) VALUES ('cleanup -- see #12'); DROP TABLE gateway_usage;",
        "SELECT 'it''s -- fine'; DROP TABLE gateway_usage;",
        "INSERT INTO t VALUES ('a /* x'); TRUNCATE gateway_usage;",
        # Contents of a literal still count. `EXECUTE 'DROP …'` in a DO block is
        # the most likely way a real drop arrives, so blanking literals to remove
        # the acknowledged false positive would trade it for a false negative
        # exactly where it matters.
        "DO $$ BEGIN EXECUTE 'DROP TABLE gateway_usage'; END $$;",
    ],
)
def test_the_destructive_detector_catches_what_it_is_for(sql):
    """Without these, stubbing the detector to return `()` leaves the sweep above
    green: on a clean migration set it only ever sees negatives."""
    assert destructive_statements(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "-- we never DROP anything here\nCREATE TABLE t (a int);",
        "/* DROP is forbidden; TRUNCATE too */ CREATE TABLE t (a int);",
        "CREATE TABLE t (drop_reason TEXT, truncated_at TIMESTAMPTZ);",
        "CREATE TABLE t (a int REFERENCES p(id) ON DELETE CASCADE);",
        # Postgres block comments nest, unlike C's; the inner `*/` must not end
        # the outer comment and expose the word after it.
        "/* outer /* inner */ still a comment, DROP */ CREATE TABLE t (a int);",
    ],
)
def test_the_destructive_detector_does_not_cry_wolf(sql):
    """A comment explaining the rule, an identifier containing the word, and
    `ON DELETE CASCADE` are all safe. A guard that fires on them would be turned
    off, and then it guards nothing."""
    assert destructive_statements(sql) == ()


# --- No stored cost, and the table matches the contract (FR-028, FR-003) ------


def _declared_columns(create_table_sql: str, table: str) -> set[str]:
    """Column names from a `CREATE TABLE <table> ( … )` body.

    Takes the first identifier of each line inside the parenthesized body,
    skipping constraint clauses. Crude on purpose — a real SQL parser is a
    dependency this project will not take for a test, and the DDL it reads is in
    this repository and written to be read this way.
    """
    body = re.search(
        # The body ends at a `)` in the first column — anything more permissive
        # runs past the table and swallows the CREATE INDEX statements after it.
        rf"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{table}\s*\(\s*\n(.*?)\n\)\s*;",
        create_table_sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert body is not None, f"no CREATE TABLE for {table}"
    columns = set()
    for line in body.group(1).splitlines():
        token = line.strip().split(" ")[0].strip(",")
        if not token or not re.fullmatch(r"[a-z_][a-z0-9_]*", token):
            continue
        if token.upper() in {"PRIMARY", "CONSTRAINT", "UNIQUE", "CHECK", "FOREIGN"}:
            continue
        columns.add(token)
    return columns


def _gateway_usage_columns(code_by_filename):
    """`gateway_usage`'s column set as of the last shipped migration.

    Walks **every** migration in version order rather than naming 001 and 002.
    That distinction is the whole point: the two assertions below — no stored cost
    (FR-028) and no drift from the contract — are designated by FR-034 as guards
    that hold with no database present, and a guard that reads a hardcoded pair of
    files silently stops guarding the day someone adds `007_add_est_cost.sql`.

    `DROP COLUMN` is not handled, and deliberately: a destructive migration cannot
    ship without `allow_destructive`, so a column that appears here can only leave
    by a route this project already refuses by default.
    """
    columns: set[str] = set()
    for filename in sorted(code_by_filename):
        sql = code_by_filename[filename]
        # `\b` keeps `gateway_usage_daily` out of this: the character after the
        # table name there is `_`, which is a word character, so it does not match.
        if re.search(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?gateway_usage\b",
            sql,
            re.IGNORECASE,
        ):
            columns |= _declared_columns(sql, "gateway_usage")
        for match in re.finditer(
            r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?gateway_usage\s+"
            r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z0-9_]*)",
            sql,
            re.IGNORECASE,
        ):
            columns.add(match.group(1))
    assert columns, "no shipped migration creates gateway_usage"
    return columns


@pytest.fixture(scope="module")
def gateway_usage_columns(code_by_filename):
    return _gateway_usage_columns(code_by_filename)


def test_the_column_scan_reaches_migrations_beyond_the_ones_shipped_today(
    code_by_filename,
):
    """Anti-vacuity guard for the two assertions that follow.

    If the column set were still read off 001 and 002 by name, a later migration
    adding a cost column to `gateway_usage` would leave both of them green while
    FR-028 and the contract-drift invariant were broken. This pins that the scan
    follows the migration set rather than a hardcoded pair of filenames.
    """
    hypothetical = dict(code_by_filename)
    hypothetical["007_add_est_cost.sql"] = (
        "ALTER TABLE gateway_usage ADD COLUMN IF NOT EXISTS est_cost_usd NUMERIC;"
    )

    assert "est_cost_usd" in _gateway_usage_columns(hypothetical)
    assert "est_cost_usd" not in _gateway_usage_columns(code_by_filename)


def test_the_usage_table_stores_no_cost(gateway_usage_columns):
    """Raw counts are stored; dollars are derived at report time.

    A stored dollar goes stale the moment a rate card changes, and nothing in the
    row then says which figures are stale.
    """
    offenders = sorted(
        c for c in gateway_usage_columns if any(w in c for w in ("cost", "usd", "price"))
    )
    assert offenders == [], (
        f"gateway_usage declares cost-shaped column(s) {offenders} — cost belongs "
        "in the rate card and the rollup, never on the usage row"
    )


def test_the_usage_table_holds_exactly_the_contract(gateway_usage_columns):
    """The table and the record must not drift apart silently.

    The contract is the definition of what a record is; a field added to it
    without a migration would be dropped on write with nothing to say so. This is
    the assertion that makes the reconstructed DDL trustworthy — the column set is
    not a guess, it is the contract's field set.
    """
    contract_fields = {f.name for f in fields(UsageRecord)}
    assert gateway_usage_columns == contract_fields | {"id"}


def test_the_writer_inserts_every_contract_field(gateway_usage_columns):
    """The insert column list and the table must agree, minus the generated key."""
    assert set(INSERT_COLUMNS) == gateway_usage_columns - {"id"}
    assert len(INSERT_COLUMNS) == len(set(INSERT_COLUMNS))


def test_every_rate_column_names_its_unit(code_by_filename):
    """FR-029, with no database. A rate card loaded against the wrong unit is a
    thousand-fold error and nothing in the data reveals it, so the unit lives in
    the column name — and a rename in some future `007` should fail here rather
    than in a cost report.
    """
    rate_columns = {
        match.group(1)
        for sql in code_by_filename.values()
        for match in re.finditer(
            r"^\s*([a-z_][a-z0-9_]*(?:cost|price|rate)[a-z0-9_]*)\s+NUMERIC",
            sql,
            re.IGNORECASE | re.MULTILINE,
        )
    }

    assert rate_columns, "no cost-valued columns found; the scan has stopped scanning"
    unitless = sorted(c for c in rate_columns if not c.endswith("_usd_per_mtok"))
    assert unitless == [], (
        f"cost column(s) {unitless} do not name their unit — a rate card loaded "
        "against the wrong one is a thousand-fold error with nothing in the data "
        "to reveal it"
    )


# --- Grants (FR-030) ----------------------------------------------------------


def _created_relations(code_by_filename):
    """Every table and view the shipped migrations create.

    Derived, not listed. A hardcoded trio passes forever once it is written, so a
    `007` creating a relation and forgetting its grant would sail through the very
    check meant to catch it — the same hole `_gateway_usage_columns` had.
    """
    relations: set[str] = set()
    for sql in code_by_filename.values():
        for match in re.finditer(
            r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|MATERIALIZED\s+VIEW)\s+"
            r"(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z0-9_]*)",
            sql,
            re.IGNORECASE,
        ):
            relations.add(match.group(1))
    return relations - {SCHEMA_MIGRATIONS_TABLE}


def _ungranted(code_by_filename):
    everything = "\n".join(code_by_filename.values())
    return sorted(
        relation
        for relation in _created_relations(code_by_filename)
        if not re.search(
            rf"GRANT\s+SELECT\s+ON\s+{relation}\s+TO", everything, re.IGNORECASE
        )
    )


def test_every_created_relation_is_granted_to_the_reader_role(code_by_filename):
    """Postgres grants nothing to other roles by default, so a relation with no
    grant migration is invisible to the reporting role — and the first anyone
    hears of it is a permission error in a dashboard."""
    found = _created_relations(code_by_filename)
    assert found >= {"gateway_usage", "model_pricing_rates", "gateway_usage_daily"}, (
        f"the scan lost track of a known relation; found {sorted(found)}"
    )
    assert _ungranted(code_by_filename) == []


def test_the_grant_check_reaches_a_relation_added_later(code_by_filename):
    """Anti-vacuity guard, mirroring the one on the column scan: a migration that
    creates a relation without granting it must be *caught*, not merely absent
    from a list somebody remembered to extend."""
    hypothetical = dict(code_by_filename)
    hypothetical["007_audit_log.sql"] = "CREATE TABLE IF NOT EXISTS audit_log (id BIGSERIAL);"

    assert _ungranted(hypothetical) == ["audit_log"]


def test_the_grants_degrade_rather_than_fail_without_a_role(code_by_filename):
    """A library cannot know a deployment's role names. Hardcoding the homelab's
    would make the migration set unusable in the tenant deployments ADR-0001
    exists to enable, and failing would make an unconfigured database
    un-migratable."""
    for name in ("004_reader_grant.sql", "005_gateway_usage_daily.sql"):
        code = code_by_filename[name]
        assert "current_setting('tokenweir.reader_role', true)" in code
        assert re.search(r"FROM\s+pg_roles\s+WHERE\s+rolname", code, re.IGNORECASE)
        assert "RAISE NOTICE" in code.upper()


def test_grants_are_issued_with_a_quoted_identifier(code_by_filename):
    """`format('… %I', role)` rather than string concatenation: the role name is
    configuration, and configuration reaching a statement unquoted is how an
    identifier becomes an injection."""
    for name in ("004_reader_grant.sql", "005_gateway_usage_daily.sql"):
        code = code_by_filename[name]
        assert re.search(r"format\('GRANT SELECT ON \w+ TO %I'", code)


# --- Idempotence (FR-005) -----------------------------------------------------


def test_the_shipped_sql_parses_as_postgres(migrations):
    """Syntax, checked against Postgres's own grammar without a Postgres.

    `pglast` wraps libpg_query — the server's real parser — so a typo in the DDL
    is caught here rather than on a deploy. It is not a substitute for
    `test_postgres_integration.py`: parsing says the statement is well-formed, not
    that it does the right thing, and semantic errors (a column that does not
    exist, a STABLE expression in an index) are invisible to it.

    Skipped where pglast is absent — it is in the `dev` extra, not a hard
    dependency, because a compiled parser is a heavy thing to require of someone
    who only wants to import the contract. `pip install -e '.[dev]'` runs it.
    """
    pglast = pytest.importorskip(
        "pglast", reason="pglast is not installed (`pip install -e '.[dev]'`)"
    )
    for migration in migrations:
        try:
            statements = pglast.parse_sql(migration.sql)
        except Exception as exc:  # pragma: no cover - the failure is the finding
            pytest.fail(f"{migration.filename} is not valid Postgres: {exc}")
        assert statements, f"{migration.filename} contains no statements"


def test_the_plpgsql_blocks_parse(migrations):
    """The grant blocks are PL/pgSQL inside a string literal, so the SQL parse
    above sees them only as text. Parsing the body is a separate call."""
    pytest.importorskip("pglast", reason="pglast is not installed")
    from pglast.parser import parse_plpgsql_json

    blocks = 0
    for migration in migrations:
        for match in re.finditer(r"DO \$\$.*?\$\$;", migration.sql, re.DOTALL):
            blocks += 1
            try:
                parse_plpgsql_json(match.group(0))
            except Exception as exc:  # pragma: no cover - the failure is the finding
                pytest.fail(f"{migration.filename} has invalid PL/pgSQL: {exc}")
    assert blocks == 2, "expected the two reader-grant blocks"


def test_every_migration_is_individually_idempotent(code_by_filename):
    """Running the migrator on every deploy has to be safe, and a database in an
    unexpected state has to be repairable rather than wedged."""
    for name, code in code_by_filename.items():
        for statement, guard in (
            (r"CREATE\s+TABLE", r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS"),
            (r"CREATE\s+INDEX", r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS"),
            (r"ADD\s+COLUMN", r"ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS"),
        ):
            total = len(re.findall(statement, code, re.IGNORECASE))
            guarded = len(re.findall(guard, code, re.IGNORECASE))
            assert total == guarded, f"{name} has an unguarded {statement!r}"
        views = len(re.findall(r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW", code, re.IGNORECASE))
        replaceable = len(re.findall(r"CREATE\s+OR\s+REPLACE\s+VIEW", code, re.IGNORECASE))
        assert views == replaceable, f"{name} creates a view without OR REPLACE"


# --- Packaging (FR-002, SC-001) -----------------------------------------------
#
# The migrations must reach a wheel install. If they do not, `tokenweir` cannot
# apply the schema it owns, and the failure is invisible from a source checkout —
# where the files are on disk regardless of what the build declares.

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_the_sql_is_readable_as_package_data():
    """Through `importlib.resources`, not `Path(__file__)`.

    The distinction is what makes the migrations readable from a wheel or a zip.
    """
    sql_dir = resources.files("tokenweir.migrations") / "sql"
    names = sorted(entry.name for entry in sql_dir.iterdir() if entry.name.endswith(".sql"))
    assert names == [m.filename for m in discover()]
    assert all((sql_dir / name).read_text(encoding="utf-8").strip() for name in names)


def test_the_build_declares_the_sql_as_package_data(migrations):
    """A source checkout cannot tell you whether the wheel would carry these —
    the files are on disk either way. This reads the declaration that decides it,
    and checks every shipped migration actually matches one of its patterns, so a
    seventh migration put somewhere the pattern misses fails here rather than on
    somebody's `pip install`.

    Skipped without `pyproject.toml`, which an installed sdist has no reason to
    keep — a check that cannot be evaluated must not turn an install red.
    """
    pyproject = REPO_ROOT / "pyproject.toml"
    if not pyproject.is_file():
        pytest.skip("no pyproject.toml (e.g. an installed distribution)")

    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    patterns = (
        config.get("tool", {})
        .get("setuptools", {})
        .get("package-data", {})
        .get("tokenweir.migrations")
    )
    assert patterns, (
        "pyproject.toml does not declare package-data for tokenweir.migrations — "
        "the wheel would ship the runner without the SQL it applies"
    )
    for migration in migrations:
        relative = f"sql/{migration.filename}"
        assert any(fnmatch(relative, pattern) for pattern in patterns), (
            f"{relative} matches no declared package-data pattern {patterns}"
        )


def test_an_actually_built_wheel_carries_the_sql(migrations, tmp_path):
    """SC-001 as written: discovered from the *installed package*, no repo present.

    The two checks above are proxies. One reads an editable install, where the
    repository is on disk whatever the build does; the other reads the
    declaration rather than the artifact. Both stay green if the declaration is
    right and the build stops honouring it — a `MANIFEST.in`, an `include-package
    -data` flip, a backend upgrade. This builds the wheel and looks inside it.

    Skipped where the build backend is not installed, which includes a bare
    install: a check that cannot be evaluated must not turn one red.
    """
    if not (REPO_ROOT / "pyproject.toml").is_file():
        pytest.skip("no pyproject.toml (e.g. an installed distribution)")
    pytest.importorskip("build", reason="`pip install build` to check the wheel itself")

    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(tmp_path), str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr[-3000:]

    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"
    with zipfile.ZipFile(wheels[0]) as wheel:
        packaged = {
            name
            for name in wheel.namelist()
            if name.startswith("tokenweir/migrations/sql/") and name.endswith(".sql")
        }

    assert packaged == {f"tokenweir/migrations/sql/{m.filename}" for m in migrations}


@pytest.mark.parametrize(
    "artifact", ["build/lib/tokenweir/postgres.py", "dist/tokenweir-0.0.0.whl"]
)
def test_building_a_wheel_does_not_dirty_the_tree(artifact):
    """Verifying that the migrations reach a wheel means building one, and
    setuptools leaves `build/` behind when you do. Unignored, that output is one
    `git add -A` from being committed — the TOKWEIR-12 defect, in a directory
    TOKWEIR-12 had no reason to anticipate.

    Uses `git check-ignore` on a pathname rather than generating the artifact, so
    it costs nothing and does not depend on a build having been run. Skips outside
    a git checkout, like the rest of this repository's hygiene checks.
    """
    import shutil
    import subprocess

    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not available")
    toplevel = subprocess.run(
        [git, "-C", str(REPO_ROOT), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if toplevel.returncode != 0 or Path(toplevel.stdout.strip()).resolve() != REPO_ROOT:
        pytest.skip(f"{REPO_ROOT} is not the root of its git repository")

    result = subprocess.run(
        [git, "-C", str(REPO_ROOT), "check-ignore", "-v", "--no-index", artifact],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{artifact} is not ignored; a wheel build dirties the tree"
    assert result.stdout.split(":", 1)[0] == ".gitignore"


# --- The README's load-bearing warnings (FR-035) ------------------------------
#
# Checked by marker string rather than by wording, so the prose stays free to
# change and the check stays cheap — the same treatment TOKWEIR-15 gave FR-022.


@pytest.mark.parametrize(
    "marker",
    [
        "python -m tokenweir.migrations apply",       # how to apply
        "TOKENWEIR_TEST_DSN",                         # how to run the store tests
        "allow_destructive=True",                     # the DROP escape hatch
        "no cost column",                             # cost is derived, never stored
        "BOOL_AND",                                   # the unpriced-group rule
        "Subscription usage is never priced",         # ADR-0001 Pillar 4
        "_usd_per_mtok",                              # the rate unit
        "`Source.write` may raise",                   # the asymmetry with Sink.emit
    ],
)
def test_the_readme_documents_the_store(marker):
    readme = REPO_ROOT / "README.md"
    if not readme.is_file():
        pytest.skip("no README.md (e.g. an installed distribution)")
    assert marker in readme.read_text(encoding="utf-8"), (
        f"README.md no longer documents {marker!r}"
    )
