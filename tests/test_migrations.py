"""The migration runner's semantics (TOKWEIR-5).

Driven through a **recording fake connection**, which is a protocol double and
nothing more: it pins the order and the transactional grouping of the statements
the runner issues, and it knows just enough about `schema_migrations` to answer
the runner's own state query. It does not model Postgres, and no assertion here
depends on it doing so.

That distinction is the project's real-Postgres test pattern, not an exception to
it. Whether the DDL is *correct* is asserted against a live server in
`test_postgres_integration.py`; whether the runner *sequences* it correctly is a
property of this module and is asserted here — where it can be checked with no
database, so a bare install still holds the line.

Sequencing is not the same as safety, and this module cannot tell the difference.
The statement order it pins looked correct while `CREATE TABLE IF NOT EXISTS
schema_migrations` was being issued outside the advisory lock, because whether two
sessions collide is a fact about Postgres, not about the order one session emits
statements in. That defect was caught by the integration suite; run it.
"""

import logging

import pytest

from tokenweir.migrations import (
    ADVISORY_LOCK_KEY,
    DestructiveMigrationError,
    Migration,
    MigrationChecksumError,
    UnknownAppliedVersionError,
    applied_versions,
    apply,
    destructive_statements,
    discover,
    pending,
    status,
)

SHIPPED = discover()
LAST_VERSION = SHIPPED[-1].version


class FakeCursor:
    def __init__(self, connection):
        self._connection = connection
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self._connection.events.append(("execute", sql, params))
        if self._connection.fail_on is not None and self._connection.fail_on(sql):
            raise RuntimeError("simulated store failure")

        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT version, checksum FROM schema_migrations"):
            self._rows = [
                (version, checksum)
                for version, (_, checksum) in sorted(self._connection.applied.items())
            ]
        elif normalized.startswith("INSERT INTO schema_migrations"):
            version, name, checksum = params
            self._connection.staged[version] = (name, checksum)
        else:
            self._rows = []

    def executemany(self, sql, rows):  # pragma: no cover - writer-side only
        for row in rows:
            self.execute(sql, row)

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    """Records what the runner does, and remembers `schema_migrations` rows.

    `staged` versus `applied` is the whole point: an insert is only visible to a
    later read once `commit` has been called, so a test can tell "recorded as
    applied" apart from "recorded and then rolled back".
    """

    def __init__(self, applied=None, fail_on=None):
        self.applied = dict(applied or {})
        self.staged = {}
        self.events = []
        self.fail_on = fail_on
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.events.append(("commit", None, None))
        self.applied.update(self.staged)
        self.staged.clear()

    def rollback(self):
        self.events.append(("rollback", None, None))
        self.staged.clear()

    def close(self):
        self.closed = True


def applied_state(migrations):
    """`{version: (name, checksum)}` as the runner would have recorded it."""
    return {m.version: (m.name, m.checksum) for m in migrations}


def statements(connection):
    return [sql for kind, sql, _ in connection.events if kind == "execute"]


def migration_sql_applied(connection):
    """Filenames of the migrations whose SQL was actually executed.

    Matched by a marker each migration carries in its leading comment — the
    version number — rather than by comparing whole texts, so the assertion says
    "this migration ran" rather than re-encoding its content.
    """
    executed = statements(connection)
    return [m.filename for m in SHIPPED if any(sql == m.sql for sql in executed)]


# --- Discovery (FR-002, SC-001) -----------------------------------------------


def test_discovery_reads_the_packaged_sql():
    """Through `importlib.resources`, so a wheel install can apply them."""
    assert [m.version for m in SHIPPED] == [1, 2, 3, 4, 5, 6]
    assert all(m.sql.strip() for m in SHIPPED)
    assert all(m.filename == f"{m.version:03d}_{m.name}.sql" for m in SHIPPED)


def test_a_migrations_checksum_follows_its_content():
    a = Migration(version=1, name="x", sql="CREATE TABLE t (a int);")
    b = Migration(version=1, name="x", sql="CREATE TABLE t (a int); -- edited")
    assert a.checksum != b.checksum
    assert a.checksum == Migration(version=9, name="y", sql=a.sql).checksum


# --- Applying (FR-008, FR-009, SC-002, SC-003) --------------------------------


def test_an_empty_database_gets_everything_in_order():
    conn = FakeConnection()
    applied = apply(conn)
    assert [m.version for m in applied] == [1, 2, 3, 4, 5, 6]
    assert migration_sql_applied(conn) == [m.filename for m in SHIPPED]
    assert set(conn.applied) == {1, 2, 3, 4, 5, 6}


def test_each_migration_commits_with_its_own_state_row():
    """One transaction per migration: "recorded as applied" and "actually
    applied" cannot disagree, because they commit together."""
    conn = FakeConnection()
    apply(conn, advisory_lock=False)

    for migration in SHIPPED:
        kinds = [(kind, sql) for kind, sql, _ in conn.events]
        ddl = kinds.index(("execute", migration.sql))
        insert = next(
            i
            for i, (kind, sql) in enumerate(kinds)
            if kind == "execute" and i > ddl and sql.startswith("INSERT INTO schema_migrations")
        )
        commit = next(i for i, (kind, _) in enumerate(kinds) if kind == "commit" and i > insert)
        between = [kind for kind, _ in kinds[ddl:commit]]
        assert "commit" not in between, f"{migration.filename} commits before its state row"
        assert "rollback" not in between


def test_re_applying_an_up_to_date_database_does_nothing():
    conn = FakeConnection(applied=applied_state(SHIPPED))
    before = dict(conn.applied)
    assert apply(conn) == ()
    assert migration_sql_applied(conn) == []
    assert conn.applied == before


def test_a_partially_migrated_database_gets_only_the_rest():
    conn = FakeConnection(applied=applied_state(SHIPPED[:3]))
    applied = apply(conn)
    assert [m.version for m in applied] == [4, 5, 6]
    assert migration_sql_applied(conn) == [m.filename for m in SHIPPED[3:]]


def test_a_failing_migration_leaves_neither_its_ddl_nor_its_row():
    target = SHIPPED[3]
    conn = FakeConnection(fail_on=lambda sql: sql == target.sql)

    with pytest.raises(RuntimeError, match="simulated store failure"):
        apply(conn)

    assert ("rollback", None, None) in conn.events
    assert set(conn.applied) == {1, 2, 3}
    assert target.version not in conn.applied
    assert conn.staged == {}


def test_pending_is_what_apply_will_do():
    conn = FakeConnection(applied=applied_state(SHIPPED[:2]))
    expected = [m.version for m in pending(conn)]
    assert expected == [3, 4, 5, 6]


def test_status_reports_both_sides():
    conn = FakeConnection(applied=applied_state(SHIPPED[:2]))
    done, outstanding = status(conn)
    assert [m.version for m in done] == [1, 2]
    assert [m.version for m in outstanding] == [3, 4, 5, 6]


def test_applied_versions_creates_its_own_state_table():
    conn = FakeConnection()
    assert applied_versions(conn) == {}
    assert any(
        "CREATE TABLE IF NOT EXISTS schema_migrations" in " ".join(sql.split())
        for sql in statements(conn)
    )


# --- Drift detection (FR-004, FR-014, SC-005) ---------------------------------


def test_a_database_ahead_of_the_library_is_refused():
    """Applying on top of a schema the library cannot describe is guessing, and
    guessing is how two consumers of a shared schema quietly diverge."""
    ahead = applied_state(SHIPPED)
    ahead[LAST_VERSION + 1] = ("future_thing", "deadbeef")
    conn = FakeConnection(applied=ahead)

    with pytest.raises(UnknownAppliedVersionError, match=str(LAST_VERSION + 1)):
        apply(conn)
    assert migration_sql_applied(conn) == []


def test_an_edited_released_migration_is_refused():
    """Forward-only means a released migration is immutable. An edit makes the
    database and the library disagree about what version N *is*, and no amount of
    re-running resolves it."""
    tampered = applied_state(SHIPPED)
    tampered[3] = (SHIPPED[2].name, "not-the-shipped-checksum")
    conn = FakeConnection(applied=tampered)

    with pytest.raises(MigrationChecksumError, match=SHIPPED[2].filename):
        apply(conn)
    assert migration_sql_applied(conn) == []


def test_the_state_table_is_brought_up_to_shape_for_a_database_that_predates_us():
    """`CREATE TABLE IF NOT EXISTS` no-ops against the AI Gateway's own
    `schema_migrations`, which has no `checksum` column — so the create alone
    leaves the very next read to fail. The `ADD COLUMN IF NOT EXISTS` statements
    are what make adopting an existing table the ordinary path.

    Pinned here, with no database, because the pod this suite runs in has none and
    the adoption path is the one TOKWEIR-10 walks first.
    """
    conn = FakeConnection()
    applied_versions(conn)
    issued = [" ".join(sql.split()) for sql in statements(conn)]

    for column in ("name", "checksum", "applied_at"):
        assert any(
            statement.startswith(
                f"ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS {column} "
            )
            for statement in issued
        ), f"nothing brings a pre-existing state table up to shape for {column!r}"

    create = next(i for i, s in enumerate(issued) if s.startswith("CREATE TABLE"))
    alters = [i for i, s in enumerate(issued) if s.startswith("ALTER TABLE")]
    assert all(i > create for i in alters), "the table must exist before it is altered"


def test_a_version_applied_without_a_checksum_is_adopted_not_refused(caplog):
    """A row the gateway's runner wrote has no checksum to compare against.
    Treating that absence as a mismatch would refuse every database this package
    exists to take over; inventing a checksum for it would be worse."""
    adopted = {version: (name, None) for version, (name, _) in applied_state(SHIPPED[:3]).items()}
    conn = FakeConnection(applied=adopted)

    with caplog.at_level(logging.WARNING, logger="tokenweir.migrations"):
        applied = apply(conn)

    assert [m.version for m in applied] == [4, 5, 6]
    assert any(
        "without a recorded checksum" in record.message for record in caplog.records
    ), "adopting rows silently would hide that they were never verified"


def test_an_adopted_row_does_not_excuse_a_genuinely_edited_one():
    """The NULL-checksum skip is narrow: it must not become a way for real drift
    to ride along beside an adopted row."""
    mixed = {version: (name, None) for version, (name, _) in applied_state(SHIPPED[:2]).items()}
    mixed[3] = (SHIPPED[2].name, "not-the-shipped-checksum")
    conn = FakeConnection(applied=mixed)

    with pytest.raises(MigrationChecksumError, match=SHIPPED[2].filename):
        apply(conn)
    assert migration_sql_applied(conn) == []


def test_checksum_verification_can_be_turned_off_to_inspect_a_drifted_database():
    """`status` defaults to not verifying for exactly this reason: being refused a
    *description* of a database that has drifted is the opposite of helpful."""
    tampered = applied_state(SHIPPED)
    tampered[3] = (SHIPPED[2].name, "not-the-shipped-checksum")
    conn = FakeConnection(applied=tampered)

    done, outstanding = status(conn)
    assert [m.version for m in done] == [1, 2, 3, 4, 5, 6]
    assert outstanding == ()


# --- No DROP without operator review (FR-010, SC-004) -------------------------


def test_a_destructive_migration_is_refused_before_anything_is_applied(monkeypatch):
    """Refused up front, not when reached: a destructive migration late in the set
    must not leave the safe ones ahead of it half-applied."""
    destructive = Migration(version=7, name="drop_the_log", sql="DROP TABLE gateway_usage;")
    monkeypatch.setattr(
        "tokenweir.migrations.discover", lambda: (*SHIPPED, destructive)
    )
    conn = FakeConnection()

    with pytest.raises(DestructiveMigrationError, match="drop_the_log"):
        apply(conn)

    assert migration_sql_applied(conn) == []
    assert conn.applied == {}


def test_an_operator_can_allow_a_destructive_migration(monkeypatch):
    """A confirmation, not a prohibition — and one that appears at the call site a
    reviewer reads, rather than in a setting someone changed last year."""
    destructive = Migration(version=7, name="drop_the_log", sql="DROP TABLE gateway_usage;")
    monkeypatch.setattr(
        "tokenweir.migrations.discover", lambda: (*SHIPPED, destructive)
    )
    conn = FakeConnection()

    applied = apply(conn, allow_destructive=True)
    assert [m.version for m in applied] == [1, 2, 3, 4, 5, 6, 7]
    assert destructive.sql in statements(conn)


def test_the_refusal_names_what_it_found(monkeypatch):
    destructive = Migration(version=7, name="cleanup", sql="TRUNCATE gateway_usage;")
    monkeypatch.setattr(
        "tokenweir.migrations.discover", lambda: (*SHIPPED, destructive)
    )
    with pytest.raises(DestructiveMigrationError) as excinfo:
        apply(FakeConnection())
    assert "TRUNCATE" in str(excinfo.value)
    assert "007_cleanup.sql" in str(excinfo.value)


def test_destructive_statements_returns_what_it_matched():
    assert destructive_statements("DROP TABLE t; TRUNCATE u;") == ("DROP", "TRUNCATE")
    assert destructive_statements("SELECT 1;") == ()


# --- The advisory lock (FR-012, SC-006) ---------------------------------------


def test_the_run_is_serialized_by_an_advisory_lock():
    conn = FakeConnection()
    apply(conn)
    locks = [
        (sql, params)
        for kind, sql, params in conn.events
        if kind == "execute" and "pg_advisory" in sql
    ]
    assert [sql for sql, _ in locks] == [
        "SELECT pg_advisory_lock(%s)",
        "SELECT pg_advisory_unlock(%s)",
    ]
    assert {params for _, params in locks} == {(ADVISORY_LOCK_KEY,)}


def test_the_lock_is_released_even_when_a_migration_fails():
    conn = FakeConnection(fail_on=lambda sql: sql == SHIPPED[2].sql)
    with pytest.raises(RuntimeError):
        apply(conn)
    assert any(
        kind == "execute" and "pg_advisory_unlock" in sql for kind, sql, _ in conn.events
    )


def test_the_lock_is_taken_before_the_first_migration():
    conn = FakeConnection()
    apply(conn)
    order = [sql for sql in statements(conn)]
    assert order.index("SELECT pg_advisory_lock(%s)") < order.index(SHIPPED[0].sql)


def test_the_lock_is_taken_before_the_state_table_is_created():
    """Creating `schema_migrations` is itself the race the lock exists to settle:
    `CREATE TABLE IF NOT EXISTS` is not atomic against a concurrent creation, so
    two migrators meeting a fresh database both see "absent" and one dies on a
    duplicate-key error from the catalog. Reading pending state before locking —
    which is what this used to do — put that statement outside the lock."""
    conn = FakeConnection()
    apply(conn)
    order = [" ".join(sql.split()) for sql in statements(conn)]

    lock = order.index("SELECT pg_advisory_lock(%s)")
    create = next(
        i
        for i, sql in enumerate(order)
        if sql.startswith("CREATE TABLE IF NOT EXISTS schema_migrations")
    )
    assert lock < create, "the state table is created outside the advisory lock"


def test_skipping_the_lock_is_possible_but_says_so(caplog):
    """A store with no advisory lock is a real case; a silent downgrade is not an
    acceptable way to serve it."""
    conn = FakeConnection()
    with caplog.at_level(logging.WARNING, logger="tokenweir.migrations"):
        apply(conn, advisory_lock=False)
    assert not any("pg_advisory" in sql for sql in statements(conn))
    assert any("advisory lock" in record.message for record in caplog.records)


def test_work_done_while_waiting_for_the_lock_is_not_repeated():
    """The re-read after acquiring the lock is the reason for taking it.

    Simulates the race: the other migrator finished while this one blocked, so by
    the time the lock is held there is nothing left to do.
    """
    conn = FakeConnection()
    real_cursor = conn.cursor

    def cursor_that_races():
        cursor = real_cursor()
        original = cursor.execute

        def execute(sql, params=None):
            original(sql, params)
            if sql == "SELECT pg_advisory_lock(%s)":
                conn.applied.update(applied_state(SHIPPED))

        cursor.execute = execute
        return cursor

    conn.cursor = cursor_that_races
    assert apply(conn) == ()
    assert migration_sql_applied(conn) == []


# --- The reader role (FR-030) -------------------------------------------------


def test_the_reader_role_is_set_for_each_migration_and_is_local():
    conn = FakeConnection()
    apply(conn, reader_role="metrics_reader")
    settings = [
        params
        for kind, sql, params in conn.events
        if kind == "execute" and "set_config" in sql
    ]
    assert settings == [("metrics_reader",)] * len(SHIPPED)
    assert all(
        "'tokenweir.reader_role'" in sql and "true" in sql
        for kind, sql, _ in conn.events
        if kind == "execute" and "set_config" in sql
    )


def test_the_role_is_passed_as_a_parameter_not_formatted_into_the_statement():
    """A role name is configuration, and configuration concatenated into a
    statement is how an identifier becomes an injection."""
    conn = FakeConnection()
    apply(conn, reader_role="robert'); DROP TABLE gateway_usage; --")
    for kind, sql, params in conn.events:
        if kind == "execute" and "set_config" in sql:
            assert "robert" not in sql
            assert params == ("robert'); DROP TABLE gateway_usage; --",)


def test_without_a_reader_role_nothing_is_set():
    conn = FakeConnection()
    apply(conn)
    assert not any("set_config" in sql for sql in statements(conn))
