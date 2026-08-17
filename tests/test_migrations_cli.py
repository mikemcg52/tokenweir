"""`python -m tokenweir.migrations` (TOKWEIR-5).

The replacement for the gateway's `scripts/migrate.py`, and the thing an operator
runs on a deploy. Two properties are worth pinning: the options work wherever a
person naturally puts them, and a failure comes out as a line an operator can read
rather than a traceback through psycopg.

The connection is stubbed out — this file is about argument handling and exit
codes, not about the database. What the runner then does with a real connection is
`test_migrations.py` and `test_postgres_integration.py`.
"""

import pytest

from tokenweir.migrations import __main__ as cli
from tokenweir.migrations import discover

SHIPPED = discover()


class StubCursor:
    def __init__(self, connection):
        self._connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self._connection.executed.append(sql)

    def fetchall(self):
        return []


class StubConnection:
    """Enough of a connection for the CLI: an empty, migratable database."""

    def __init__(self):
        self.executed = []
        self.closed = False

    def cursor(self):
        return StubCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def stub_connect(monkeypatch):
    """Replaces `connect`, and records the DSN it was asked for."""
    calls = []

    def connect(dsn, **kwargs):
        calls.append(dsn)
        return StubConnection()

    monkeypatch.setattr(cli, "connect", connect)
    return calls


@pytest.fixture(autouse=True)
def no_ambient_environment(monkeypatch):
    """A developer's own $TOKENWEIR_DSN must not decide what these assert."""
    monkeypatch.delenv(cli.DSN_ENV_VAR, raising=False)
    monkeypatch.delenv(cli.READER_ROLE_ENV_VAR, raising=False)


# --- Where the options may go -------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["status", "--dsn", "postgresql:///scratch"],
        ["--dsn", "postgresql:///scratch", "status"],
    ],
)
def test_the_dsn_is_accepted_on_either_side_of_the_subcommand(argv, stub_connect, capsys):
    """`... status --dsn X` is the form anyone writes first, and argparse rejects
    it by default. The subparser must also not overwrite a DSN given before the
    subcommand with its own default — the failure mode that makes the two-position
    fix look like it works while quietly discarding one of them."""
    assert cli.main(argv) == 0
    assert stub_connect == ["postgresql:///scratch"]
    assert "pending:" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["apply", "--dsn", "postgresql:///s", "--reader-role", "rdr"],
        ["--reader-role", "rdr", "apply", "--dsn", "postgresql:///s"],
        ["--dsn", "postgresql:///s", "--reader-role", "rdr", "apply"],
    ],
)
def test_the_reader_role_is_accepted_on_either_side_of_the_subcommand(
    argv, stub_connect, monkeypatch
):
    """FR-013 says connection options work on either side, not just `--dsn`. The
    three options share one parent parser, so this is coverage rather than a
    separate mechanism — but the parent parser is exactly the kind of thing a
    later edit breaks for one option while leaving the tested one green."""
    seen = {}
    monkeypatch.setattr(cli, "apply", lambda connection, **kwargs: seen.update(kwargs) or ())

    assert cli.main(argv) == 0
    assert seen["reader_role"] == "rdr"
    assert stub_connect == ["postgresql:///s"]


@pytest.mark.parametrize(
    "argv",
    [
        ["status", "--dsn", "postgresql:///s", "--verbose"],
        ["--verbose", "status", "--dsn", "postgresql:///s"],
    ],
)
def test_verbose_is_accepted_on_either_side_of_the_subcommand(argv):
    """`--verbose` is `store_true` with a suppressed default — the combination
    most likely to come out as "always false" if the parent parser is reworked.

    Asserted against the parsed namespace rather than the logging it turns on:
    `logging.basicConfig` is a no-op once pytest has installed a root handler, so
    an assertion on the log level would pass whatever the parser did.
    """
    assert getattr(cli._build_parser().parse_args(argv), "verbose", False) is True


def test_verbose_is_absent_rather_than_false_when_not_given():
    """The suppressed default is load-bearing: if it were a real `False`, the
    subparser would write it over a `--verbose` given before the subcommand."""
    args = cli._build_parser().parse_args(["status", "--dsn", "postgresql:///s"])
    assert not hasattr(args, "verbose")


def test_the_later_dsn_wins_and_neither_position_is_discarded(stub_connect):
    """SC-022's remaining case: given on *both* sides, one of them has to lose,
    and it must lose to the other's value rather than to a default that silently
    replaces both."""
    assert (
        cli.main(["--dsn", "postgresql:///first", "status", "--dsn", "postgresql:///second"])
        == 0
    )
    assert stub_connect == ["postgresql:///second"]


def test_the_dsn_falls_back_to_the_environment(stub_connect, monkeypatch):
    monkeypatch.setenv(cli.DSN_ENV_VAR, "postgresql:///from-env")
    assert cli.main(["status"]) == 0
    assert stub_connect == ["postgresql:///from-env"]


def test_an_explicit_dsn_beats_the_environment(stub_connect, monkeypatch):
    monkeypatch.setenv(cli.DSN_ENV_VAR, "postgresql:///from-env")
    assert cli.main(["status", "--dsn", "postgresql:///explicit"]) == 0
    assert stub_connect == ["postgresql:///explicit"]


def test_the_reader_role_reaches_apply(stub_connect, monkeypatch):
    seen = {}

    def fake_apply(connection, **kwargs):
        seen.update(kwargs)
        return ()

    monkeypatch.setattr(cli, "apply", fake_apply)
    assert cli.main(["apply", "--dsn", "postgresql:///s", "--reader-role", "rdr"]) == 0
    assert seen["reader_role"] == "rdr"
    assert seen["allow_destructive"] is False
    assert seen["advisory_lock"] is True


def test_the_flags_that_relax_the_guards_reach_apply(stub_connect, monkeypatch):
    seen = {}

    def fake_apply(connection, **kwargs):
        seen.update(kwargs)
        return ()

    monkeypatch.setattr(cli, "apply", fake_apply)
    assert (
        cli.main(
            [
                "apply",
                "--dsn",
                "postgresql:///s",
                "--allow-destructive",
                "--no-advisory-lock",
            ]
        )
        == 0
    )
    assert seen["allow_destructive"] is True
    assert seen["advisory_lock"] is False


# --- What it prints -----------------------------------------------------------


def test_status_reports_all_three_lists(stub_connect, capsys):
    assert cli.main(["status", "--dsn", "postgresql:///s"]) == 0
    out = capsys.readouterr().out
    assert "shipped:" in out and "applied:" in out and "pending:" in out
    for migration in SHIPPED:
        assert migration.filename in out


def test_apply_names_what_it_applied(stub_connect, capsys):
    assert cli.main(["apply", "--dsn", "postgresql:///s"]) == 0
    out = capsys.readouterr().out
    assert all(m.filename in out for m in SHIPPED)


def test_apply_says_so_when_there_is_nothing_to_do(stub_connect, monkeypatch, capsys):
    monkeypatch.setattr(cli, "apply", lambda connection, **kwargs: ())
    assert cli.main(["apply", "--dsn", "postgresql:///s"]) == 0
    assert "already up to date" in capsys.readouterr().out


# --- Failures are messages, not tracebacks ------------------------------------


def test_no_dsn_is_a_message_and_a_non_zero_exit(capsys):
    assert cli.main(["status"]) == 1
    assert cli.DSN_ENV_VAR in capsys.readouterr().err


def test_an_unreachable_database_is_a_message_not_a_traceback(monkeypatch, capsys):
    """An operator reading a deploy log is not debugging this library."""

    def refuse(dsn, **kwargs):
        raise OSError("could not connect to server: Connection refused")

    monkeypatch.setattr(cli, "connect", refuse)
    assert cli.main(["status", "--dsn", "postgresql:///nope"]) == 1
    err = capsys.readouterr().err
    assert "could not connect" in err
    assert "Traceback" not in err


def test_a_missing_driver_names_the_extra(monkeypatch, capsys):
    def no_driver(dsn, **kwargs):
        raise ImportError("install it with `pip install 'tokenweir[postgres]'`")

    monkeypatch.setattr(cli, "connect", no_driver)
    assert cli.main(["status", "--dsn", "postgresql:///nope"]) == 1
    assert "tokenweir[postgres]" in capsys.readouterr().err


def test_a_refusal_from_the_runner_is_reported_as_one(stub_connect, monkeypatch, capsys):
    from tokenweir.migrations import DestructiveMigrationError

    def refuse(connection, **kwargs):
        raise DestructiveMigrationError("refusing to apply destructive migration(s)")

    monkeypatch.setattr(cli, "apply", refuse)
    assert cli.main(["apply", "--dsn", "postgresql:///s"]) == 1
    err = capsys.readouterr().err
    assert "refusing to apply destructive" in err
    assert "Traceback" not in err


def test_the_connection_is_closed_even_when_the_command_fails(monkeypatch):
    opened = []

    def connect(dsn, **kwargs):
        connection = StubConnection()
        opened.append(connection)
        return connection

    monkeypatch.setattr(cli, "connect", connect)
    monkeypatch.setattr(cli, "apply", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    assert cli.main(["apply", "--dsn", "postgresql:///s"]) == 1
    assert opened and all(connection.closed for connection in opened)


def test_an_unknown_subcommand_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["bogus"])
    assert excinfo.value.code == 2


def test_no_subcommand_is_a_usage_error():
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code == 2
