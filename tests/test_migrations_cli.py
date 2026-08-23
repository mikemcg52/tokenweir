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

from tokenweir import reconcile
from tokenweir.migrations import MigrationChecksumError, discover
from tokenweir.migrations import __main__ as cli

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


def test_status_does_not_verify_checksums_unless_asked(stub_connect, monkeypatch):
    """The default is what FR-038 requires: being refused a *description* of a
    drifted database is the opposite of helpful."""
    seen = {}
    monkeypatch.setattr(
        cli, "status", lambda connection, **kwargs: seen.update(kwargs) or ((), ())
    )

    assert cli.main(["status", "--dsn", "postgresql:///s"]) == 0
    assert seen["verify_checksums"] is False


def test_status_can_be_asked_to_verify_checksums(stub_connect, monkeypatch):
    """Without the flag there is no way to see drift from the command line, which
    is where an operator is standing when they need to."""
    seen = {}
    monkeypatch.setattr(
        cli, "status", lambda connection, **kwargs: seen.update(kwargs) or ((), ())
    )

    assert cli.main(["status", "--dsn", "postgresql:///s", "--verify-checksums"]) == 0
    assert seen["verify_checksums"] is True


def test_a_drifted_database_reported_by_status_exits_non_zero(
    stub_connect, monkeypatch, capsys
):
    """A refusal must still be a message and a non-zero exit, never a traceback
    (FR-013) — including on the path that only exists to surface a refusal."""

    def drifted(connection, **kwargs):
        raise MigrationChecksumError("001_gateway_usage.sql has been edited")

    monkeypatch.setattr(cli, "status", drifted)

    assert cli.main(["status", "--dsn", "postgresql:///s", "--verify-checksums"]) == 1
    captured = capsys.readouterr()
    assert "001_gateway_usage.sql" in captured.err
    assert "Traceback" not in captured.err


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


# --- `reconcile` (TOKWEIR-10) -------------------------------------------------
#
# Stubbed like everything above: this file is about argument handling and exit
# codes. What the reconciler does to a real database is `test_reconcile.py`, and
# nothing here is offered as a substitute for it.


def _plan(*discrepancies, baseline=None, observations=()):
    return reconcile.ReconciliationPlan(
        discrepancies=tuple(discrepancies),
        observations=tuple(observations),
        baseline_effective_from=baseline,
    )


def _automatic(statement="ALTER TABLE gateway_usage ADD COLUMN schema_version integer"):
    return reconcile.Discrepancy(
        relation="gateway_usage",
        description="column schema_version (integer) is missing",
        resolution=reconcile.Resolution.AUTOMATIC,
        statements=(statement,),
    )


def _manual():
    return reconcile.Discrepancy(
        relation="model_pricing_rates",
        description="rate card is current-valued",
        resolution=reconcile.Resolution.MANUAL,
        remedy="re-run with a baseline date",
    )


@pytest.fixture
def stub_reconciler(monkeypatch):
    """Records what the CLI asked the reconciler to do, and what it passed."""

    calls = {"plan": [], "apply": []}
    planned = _plan(_automatic())

    def fake_plan(connection, *, baseline_effective_from=None):
        calls["plan"].append(baseline_effective_from)
        return calls.get("planned", planned)

    def fake_apply(connection, existing_plan=None, **kwargs):
        calls["apply"].append(existing_plan)
        return existing_plan if existing_plan is not None else planned

    monkeypatch.setattr(cli.reconciler, "plan", fake_plan)
    monkeypatch.setattr(cli.reconciler, "apply", fake_apply)
    calls["planned"] = planned
    return calls


def test_reconcile_plans_and_changes_nothing_without_apply(
    stub_connect, stub_reconciler, capsys
):
    """The default is the safe one. An operator who types the command wrong gets a
    report, not a migration."""
    assert cli.main(["reconcile", "--dsn", "postgresql:///s"]) == 0

    assert stub_reconciler["plan"] == [None]
    assert stub_reconciler["apply"] == []
    out = capsys.readouterr().out
    assert "schema_version" in out
    assert "--apply" in out


def test_reconcile_applies_the_plan_it_printed(stub_connect, stub_reconciler, capsys):
    """Printed first and whether or not it applied, so a log of a run that changed
    the database still says what it changed."""
    assert cli.main(["reconcile", "--dsn", "postgresql:///s", "--apply"]) == 0

    assert stub_reconciler["apply"] == [stub_reconciler["planned"]]
    out = capsys.readouterr().out
    assert "schema_version" in out
    assert "reconciled: 1 discrepancy" in out


def test_reconcile_says_so_when_there_is_nothing_to_do(
    stub_connect, stub_reconciler, capsys
):
    stub_reconciler["planned"] = _plan()
    assert cli.main(["reconcile", "--dsn", "postgresql:///s", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "already matches" in out


def test_a_plan_needing_a_decision_is_a_message_and_a_non_zero_exit(
    stub_connect, stub_reconciler, monkeypatch, capsys
):
    """A refusal to act exits 1 and reads as a sentence. The reconciler's refusals
    are `ReconcileError`s, which the CLI had no reason to know about before this
    subcommand existed — without the widened `except` they would have come out as
    `error: ManualResolutionError: ...`, which is a traceback with better
    manners."""
    stub_reconciler["planned"] = _plan(_automatic(), _manual())

    def refuse(connection, existing_plan=None, **kwargs):
        raise reconcile.ManualResolutionError(
            "refusing to reconcile: 1 discrepancy needs a decision"
        )

    monkeypatch.setattr(cli.reconciler, "apply", refuse)

    assert cli.main(["reconcile", "--dsn", "postgresql:///s", "--apply"]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error: refusing to reconcile")
    assert "ManualResolutionError" not in captured.err


def test_a_plan_needing_a_decision_still_exits_zero_when_only_planning(
    stub_connect, stub_reconciler, capsys
):
    """A report succeeds even when what it reports is bad news — the same rule
    `status` follows for a database with pending migrations."""
    stub_reconciler["planned"] = _plan(_manual())
    assert cli.main(["reconcile", "--dsn", "postgresql:///s"]) == 0
    assert "MANUAL" in capsys.readouterr().out


def test_the_baseline_reaches_the_reconciler_normalised(stub_connect, stub_reconciler):
    assert (
        cli.main(
            [
                "reconcile",
                "--dsn",
                "postgresql:///s",
                "--baseline-effective-from",
                "  2024-01-01  ",
            ]
        )
        == 0
    )
    assert stub_reconciler["plan"] == ["2024-01-01"]


def test_the_documented_negative_infinity_form_reaches_the_reconciler(
    stub_connect, stub_reconciler
):
    """`-infinity` is the one value the flag documents besides a date, and argparse
    reads its leading dash as an option — so the spaced form the README used to
    show fails with "expected one argument" before the command ever connects.

    The `=` form is what works, and it is now what everything documents. This test
    exists because the recovery an operator reaches for when the spaced form fails
    is dropping the dash, and `infinity` used to be accepted — silently unpricing
    every historical row.
    """
    assert (
        cli.main(
            ["reconcile", "--dsn", "postgresql:///s", "--baseline-effective-from=-infinity"]
        )
        == 0
    )
    assert stub_reconciler["plan"] == ["-infinity"]


def test_a_positive_infinity_baseline_is_a_usage_error(stub_connect):
    """Rejected by argparse, so the command never opens a connection to find out —
    and the message says what to write instead."""
    with pytest.raises(SystemExit) as raised:
        cli.main(["reconcile", "--dsn", "postgresql:///s", "--baseline-effective-from", "infinity"])
    assert raised.value.code == 2


@pytest.mark.parametrize("given", ["not-a-date", "01/01/2024"])
def test_a_baseline_that_is_not_a_date_is_a_usage_error(given, stub_connect):
    """Exit 2, not 1: a typo in an argument is argparse's to reject, and rejecting
    it there means the command never opens a connection to find out."""
    with pytest.raises(SystemExit) as raised:
        cli.main(["reconcile", "--dsn", "postgresql:///s", "--baseline-effective-from", given])
    assert raised.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["reconcile", "--dsn", "postgresql:///scratch"],
        ["--dsn", "postgresql:///scratch", "reconcile"],
    ],
)
def test_the_dsn_is_accepted_on_either_side_of_reconcile(argv, stub_connect, stub_reconciler):
    assert cli.main(argv) == 0
    assert stub_connect == ["postgresql:///scratch"]
