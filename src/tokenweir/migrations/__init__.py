"""Schema ownership — the migrations, and the runner that applies them.

ADR-0001 Pillar 5 moves ownership of ``gateway_usage`` and its forward-only
migrations out of the AI Gateway and into this package. That is what this module
is: the six migrations ship *inside the distribution* (``tokenweir/migrations/sql/``,
read through :mod:`importlib.resources` so a wheel install works), and
:func:`apply` is how a deployment gets them into a database.

The rules the gateway earned, restated as behaviour rather than convention:

- **Forward-only.** There are no down-migrations, and a released migration is
  immutable — a change is a new version. :func:`apply` verifies the checksum of
  every already-applied migration against the shipped file, so an edited one is a
  loud failure rather than a database that quietly disagrees with the library.
- **No DROP without operator review.** :func:`apply` refuses SQL containing a
  destructive statement unless the caller passes ``allow_destructive=True`` — a
  per-call argument, so the decision is visible at the call site a reviewer reads
  rather than buried in configuration.
- **Idempotent.** Every migration is written with ``IF NOT EXISTS`` /
  ``CREATE OR REPLACE``, and applying an up-to-date database is a no-op, so
  running the migrator on every deploy is safe.
- **Atomic.** A migration's DDL and its ``schema_migrations`` row commit together,
  so "recorded as applied" and "actually applied" cannot disagree.

**The connection's transactions belong to the runner.** Every function here
commits — the runner owns its transaction boundaries, which is what makes "the
DDL and its ``schema_migrations`` row commit together" true — and taking the
advisory lock commits as well. Give it a connection of its own rather than one
with other work in flight.

**A connection, not a DSN.** Every function here takes a DB-API connection. That
keeps the driver out of this package's import graph (ADR-0001 Pillar 2 — the core
compiles in no database library), lets a deployment own pooling and credentials,
and makes the runner testable with no driver installed. :func:`connect` exists for
the convenient path and is the only place a driver is imported.

Two things the supplied connection must do, because a migration file is a script
rather than a single statement:

- accept **multiple statements in one** ``execute`` (psycopg does this when no
  parameters are passed);
- use ``%s`` **placeholders** (psycopg 2 and 3 both do).

Both are true of the drivers this project targets; a connection that does neither
belongs behind an adapter of its own.

And one thing it must **not** do: be in **autocommit** mode. :func:`apply` refuses
one. Each migration has to commit together with its ``schema_migrations`` row, and
the reader-role grant is scoped to the applying transaction — under autocommit
the first guarantee is lost and the second grant is lost *permanently*, since the
migration that would have issued it is recorded as applied. Both failures are
silent, which is why this is a refusal rather than a line in this docstring.
"""

from __future__ import annotations

import hashlib
import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from typing import Any, Iterable, Iterator, Optional, Sequence

__all__ = [
    "ADVISORY_LOCK_KEY",
    "SCHEMA_MIGRATIONS_TABLE",
    "DestructiveMigrationError",
    "MigrationChecksumError",
    "Migration",
    "UnknownAppliedVersionError",
    "applied_versions",
    "apply",
    "connect",
    "destructive_statements",
    "discover",
    "pending",
]

_logger = logging.getLogger(__name__)

#: The table the runner keeps its own state in. Created on first use.
SCHEMA_MIGRATIONS_TABLE = "schema_migrations"

#: Key for the session-level advisory lock that serializes concurrent runs.
#: An arbitrary but *stable* constant — two deploys racing must pick the same
#: number or the lock protects nothing. Derived from the package name so it is
#: reproducible rather than magic, and narrowed to a signed 64-bit value because
#: that is what ``pg_advisory_lock`` takes.
ADVISORY_LOCK_KEY = (
    int.from_bytes(hashlib.sha256(b"tokenweir.migrations").digest()[:8], "big")
    - 2**63
)

#: ``NNN_some_name.sql``. The three-digit zero padding is what makes lexical
#: order numeric order, which is what makes "apply in filename order" correct.
_FILENAME_RE = re.compile(r"^(?P<version>\d{3})_(?P<name>[a-z0-9_]+)\.sql$")

def _strip_comments(sql: str) -> str:
    """Blank out SQL comments, leaving string literals intact.

    Comments go because the prose above each migration necessarily *names* the
    things it promises not to do; read literally it would trip the guard below.

    Literals stay, contents and all. That asymmetry is the point: a ``DROP``
    inside a string is most likely an ``EXECUTE 'DROP TABLE …'``, which is
    precisely what "no DROP without operator review" is for. Blanking literals
    would turn the guard's one acknowledged false positive into a false negative
    at the only place that matters.

    Written as a scan rather than a regex because the two constructs interleave,
    and which one *opens first* decides. A regex for ``--`` to end of line does
    not know it is inside a literal, so

        INSERT INTO audit(reason) VALUES ('cleanup -- see #12'); DROP TABLE t;

    read as a comment from ``--`` onwards and hid the ``DROP`` on the same line.
    """
    out: list[str] = []
    index, end = 0, len(sql)
    while index < end:
        pair = sql[index : index + 2]
        if pair == "--":
            newline = sql.find("\n", index)
            index = end if newline == -1 else newline
            out.append(" ")
        elif pair == "/*":
            # Postgres block comments nest, unlike C's.
            depth, scan = 1, index + 2
            while scan < end and depth:
                if sql[scan : scan + 2] == "/*":
                    depth, scan = depth + 1, scan + 2
                elif sql[scan : scan + 2] == "*/":
                    depth, scan = depth - 1, scan + 2
                else:
                    scan += 1
            index = scan
            out.append(" ")
        elif sql[index] == "'":
            scan = index + 1
            while scan < end:
                if sql[scan] == "'":
                    if sql[scan + 1 : scan + 2] == "'":  # '' escapes a quote
                        scan += 2
                        continue
                    scan += 1
                    break
                scan += 1
            out.append(sql[index:scan])  # kept, not blanked
            index = scan
        else:
            out.append(sql[index])
            index += 1
    return "".join(out)

#: Whole-word matching, so an identifier that merely contains the word — a
#: ``drop_reason`` column, a ``truncated_at`` timestamp — is not a destructive
#: statement. ``DELETE`` needs its ``FROM`` because ``ON DELETE CASCADE`` in a
#: foreign key is a constraint clause, not a deletion.
_DESTRUCTIVE_RE = re.compile(
    r"\bDROP\b|\bTRUNCATE\b|\bDELETE\s+FROM\b", re.IGNORECASE
)


class MigrationError(RuntimeError):
    """Base class for the runner's refusals. All of them are refusals to act."""


class DestructiveMigrationError(MigrationError):
    """A migration contains a destructive statement and was not explicitly allowed."""


class UnknownAppliedVersionError(MigrationError):
    """The database has applied a version this library does not ship.

    The database is ahead of the library. Applying anything on top of it would be
    guessing at what is already there — which is how two consumers of a shared
    schema quietly diverge — so the runner stops instead.
    """


class MigrationChecksumError(MigrationError):
    """A released migration's content changed after it was applied.

    Migrations are forward-only: the fix for a released migration is a new
    version, never an edit to the old one. An edit means the database and the
    library disagree about what version N *is*, and no amount of re-running
    resolves that.
    """


@dataclass(frozen=True)
class Migration:
    """One numbered, forward-only migration."""

    version: int
    name: str
    sql: str

    @property
    def filename(self) -> str:
        """The name as it appears on disk, zero-padded."""
        return f"{self.version:03d}_{self.name}.sql"

    @property
    def checksum(self) -> str:
        """SHA-256 of the SQL. Recorded on apply; verified on every later run."""
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def _sql_directory() -> Any:
    """The packaged ``sql/`` directory as a traversable.

    ``importlib.resources`` rather than ``Path(__file__).parent`` because the
    migrations are package data and must be readable from a wheel or a zip — the
    library has to be able to *apply* them, unlike ``schema/usage-record.v1.json``,
    which is deliberately a repository-only artifact a consumer reads for itself.
    """
    return resources.files(__name__) / "sql"


def discover() -> tuple[Migration, ...]:
    """Load every shipped migration, in ascending version order.

    Raises:
        MigrationError: if a file in ``sql/`` is not a well-formed migration, if
            two share a version, or if the versions are not contiguous from 1.
            All three would make "apply everything pending in order" mean
            something other than what it says, and a migrator that silently
            skipped a gap would leave a database nobody can reason about.
    """
    migrations: list[Migration] = []
    for entry in sorted(_sql_directory().iterdir(), key=lambda item: item.name):
        if not entry.name.endswith(".sql"):
            continue
        match = _FILENAME_RE.match(entry.name)
        if match is None:
            raise MigrationError(
                f"{entry.name!r} is not a well-formed migration filename; "
                "expected NNN_lower_snake_name.sql with three-digit zero padding"
            )
        migrations.append(
            Migration(
                version=int(match.group("version")),
                name=match.group("name"),
                sql=entry.read_text(encoding="utf-8"),
            )
        )

    if not migrations:
        raise MigrationError("no migrations are packaged with tokenweir")

    versions = [m.version for m in migrations]
    duplicates = sorted({v for v in versions if versions.count(v) > 1})
    if duplicates:
        raise MigrationError(f"duplicate migration version(s): {duplicates}")

    expected = list(range(1, len(migrations) + 1))
    if versions != expected:
        raise MigrationError(
            f"migration versions must be contiguous from 001; got {versions}"
        )

    return tuple(migrations)


def destructive_statements(sql: str) -> tuple[str, ...]:
    """Return the destructive keywords found in ``sql``, comments excluded.

    "No DROP without operator review" as a check rather than a habit. Comments are
    stripped first and matching is whole-word, so neither a rationale that
    mentions dropping nor a column called ``drop_reason`` trips it.

    The limit worth knowing: a *string literal* containing one of these words
    matches. That is a false positive rather than a false negative — it refuses a
    safe migration instead of admitting a dangerous one — and the
    ``allow_destructive`` escape hatch covers it. It is also the right way round
    for the case that matters: ``EXECUTE 'DROP TABLE …'`` inside a ``DO`` block is
    a real drop, and blanking literals to remove the false positive would let it
    through.

    Comment-stripping is a scan, not a regex, because a ``--`` *inside* a literal
    is not a comment; treating it as one hid anything sharing its line.
    """
    return tuple(
        match.group(0) for match in _DESTRUCTIVE_RE.finditer(_strip_comments(sql))
    )


def _ensure_state_table(cursor: Any) -> None:
    """Create ``schema_migrations`` if it is absent, and adopt one that predates us.

    Not itself a migration: the runner cannot record that it created the table it
    records things in. ``IF NOT EXISTS`` makes it safe on every run.

    **Adoption.** The database this package is pointed at first is, by design, one
    the AI Gateway already migrated — that is what Pillar 5's ownership move
    *means*. Its ``schema_migrations`` predates this runner and has no ``checksum``
    column (FR-038 is ours, not the gateway's), so a bare ``CREATE TABLE IF NOT
    EXISTS`` would no-op and the very next ``SELECT version, checksum`` would fail
    with ``UndefinedColumn`` — an error that is neither actionable nor recoverable,
    and that would break ``status`` exactly when an operator needs it to describe
    the database. The ``ADD COLUMN IF NOT EXISTS`` statements below make taking
    over an existing table the ordinary path rather than a wall.

    The adopted columns are **nullable**, because existing rows have no value to
    give them and inventing one would be a lie about what was applied. A NULL
    ``checksum`` means "applied before this runner recorded checksums"; :func:`pending`
    reports those as unverifiable rather than as drift. A table this runner creates
    itself keeps ``NOT NULL``, since every row it inserts carries both.
    """
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_MIGRATIONS_TABLE} (
            version    INTEGER     PRIMARY KEY,
            name       TEXT        NOT NULL,
            checksum   TEXT        NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # Probe first, then alter only what is missing. The ``ADD COLUMN IF NOT
    # EXISTS`` statements are cheap but not free: each takes ACCESS EXCLUSIVE on
    # the table even when it does nothing, and this runs on every `status` too.
    # In the steady state — which is every run after the first — the probe finds
    # all three and this path issues no DDL at all.
    cursor.execute(
        "SELECT attname FROM pg_attribute "
        "WHERE attrelid = to_regclass(%s) AND attnum > 0 AND NOT attisdropped",
        (SCHEMA_MIGRATIONS_TABLE,),
    )
    present = {row[0] for row in cursor.fetchall()}
    for column, definition in (
        ("name", "TEXT"),
        ("checksum", "TEXT"),
        ("applied_at", "TIMESTAMPTZ NOT NULL DEFAULT now()"),
    ):
        if column in present:
            continue
        cursor.execute(
            f"ALTER TABLE {SCHEMA_MIGRATIONS_TABLE} "
            f"ADD COLUMN IF NOT EXISTS {column} {definition}"
        )


def applied_versions(
    connection: Any, *, advisory_lock: bool = True
) -> dict[int, Optional[str]]:
    """Return ``{version: checksum}`` for everything the database has applied.

    Creates ``schema_migrations`` if it does not exist, so a fresh database
    answers ``{}`` rather than raising. A version whose checksum is ``None`` was
    applied by something that did not record one — see :func:`_ensure_state_table`.

    Args:
        advisory_lock: hold the migration advisory lock while creating and reading
            the table. On by default because *this* is the function that creates
            it, and creating it is the race (FR-012). Pass ``False`` only when the
            caller already holds the lock, as :func:`apply` does.
    """
    with _advisory_lock(connection, advisory_lock):
        with connection.cursor() as cursor:
            _ensure_state_table(cursor)
            cursor.execute(
                f"SELECT version, checksum FROM {SCHEMA_MIGRATIONS_TABLE} "
                "ORDER BY version"
            )
            rows = cursor.fetchall()
        connection.commit()
    return {int(row[0]): row[1] for row in rows}


def _check_applied(
    applied: dict[int, Optional[str]],
    by_version: dict[int, Migration],
    *,
    verify_checksums: bool,
) -> None:
    """Refuse a database this library cannot honestly migrate.

    Shared by :func:`pending` and :func:`status` so the two cannot drift apart on
    what counts as a database worth refusing.
    """
    # This one has no opt-out, including from `status`, and that is a decision
    # rather than an oversight — FR-038 and FR-042 both carved one out, so the
    # asymmetry is worth stating. A drifted or adopted database is one this
    # library can still *describe*: it ships those versions and knows their names
    # and contents, so refusing to describe them would withhold something it has.
    # A database ahead of the library is not. tokenweir does not ship version N+1
    # and has no name, no content and no checksum for it; the honest report is
    # that it cannot describe this database, which is what the refusal says —
    # naming the versions and the remedy. Softening it to a warning would produce
    # a `status` that lists six applied migrations while silently omitting a
    # seventh, which is worse than a refusal.
    unknown = sorted(set(applied) - set(by_version))
    if unknown:
        raise UnknownAppliedVersionError(
            f"database has applied migration version(s) {unknown} that this "
            f"tokenweir does not ship (it ships 1..{max(by_version)}) — the "
            "database is ahead of the library; upgrade tokenweir rather than "
            "migrating on top of a schema it cannot describe"
        )

    # Announced before the `verify_checksums` early return, not after it. FR-042
    # requires unverifiable rows be reported as such, and `status` — the command
    # an operator runs precisely to have an adopted database described to them —
    # defaults to not verifying. Warning only on the verifying path meant the one
    # caller that most needs to hear it was the one guaranteed not to.
    adopted = sorted(v for v, checksum in applied.items() if checksum is None)
    if adopted:
        _logger.warning(
            "tokenweir: migration version(s) %s were applied without a recorded "
            "checksum and cannot be verified against the shipped files; they are "
            "treated as applied. This is expected the first time tokenweir takes "
            "over a schema the AI Gateway migrated.",
            adopted,
        )

    if not verify_checksums:
        return

    changed = [
        by_version[version].filename
        for version, checksum in sorted(applied.items())
        if checksum is not None and checksum != by_version[version].checksum
    ]
    if changed:
        raise MigrationChecksumError(
            "already-applied migration(s) have been edited since they were "
            f"applied: {', '.join(changed)} — migrations are forward-only, so "
            "the fix is a new version, not an edit to a released one"
        )


def pending(
    connection: Any, *, verify_checksums: bool = True, advisory_lock: bool = True
) -> tuple[Migration, ...]:
    """Return the migrations this database has not applied, in order.

    Args:
        verify_checksums: also check that every *already-applied* migration still
            matches the shipped file. On by default: it is what makes
            "forward-only, never edited" a property of the system rather than a
            note in a README.
        advisory_lock: see :func:`applied_versions`.

    Raises:
        UnknownAppliedVersionError: the database is ahead of the library.
        MigrationChecksumError: a released migration was edited after being applied.
    """
    shipped = discover()
    by_version = {m.version: m for m in shipped}
    applied = applied_versions(connection, advisory_lock=advisory_lock)
    _check_applied(applied, by_version, verify_checksums=verify_checksums)
    return tuple(m for m in shipped if m.version not in applied)


def _acquire_lock(cursor: Any) -> None:
    cursor.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))


def _release_lock(cursor: Any) -> None:
    cursor.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))


@contextmanager
def _advisory_lock(connection: Any, enabled: bool) -> Iterator[None]:
    """Hold the migration advisory lock for the duration of the block.

    Every entry point that may *create* ``schema_migrations`` goes through here,
    not just :func:`apply`. Creating that table is the race — ``CREATE TABLE IF
    NOT EXISTS`` is not atomic against a concurrent creation — and the table is
    created by whichever call arrives first, which on a fresh database is as
    likely to be a ``status`` from a health check as an ``apply`` from a deploy.
    Locking only the writer left the reader able to kill it.

    Nesting is avoided rather than relied on: :func:`apply` holds the lock and
    passes ``advisory_lock=False`` inward. Postgres's session-level advisory locks
    are reference-counted and would tolerate re-entry, but "the lock is taken in
    exactly one place per call" is a property worth being able to read off the
    code.

    **This commits the connection** on entry and on exit — so anything the caller
    had open on it commits too. Every function here already commits (the runner
    owns its transaction boundaries; that is what FR-009 is), and a migration
    connection is not one to interleave other work on. Callers who need their own
    transaction should use their own connection, or pass ``advisory_lock=False``
    and serialize the runs themselves.
    """
    if not enabled:
        yield
        return

    with connection.cursor() as cursor:
        _acquire_lock(cursor)
    connection.commit()
    try:
        yield
    finally:
        try:
            with connection.cursor() as cursor:
                _release_lock(cursor)
            connection.commit()
        except Exception:  # pragma: no cover - best effort
            # The lock is session-scoped, so closing the connection releases it
            # regardless. Failing to unlock must not mask the error that brought
            # us here.
            _logger.warning(
                "tokenweir: could not release the migration advisory lock; "
                "it is released when the connection closes",
                exc_info=True,
            )


def apply(
    connection: Any,
    *,
    reader_role: Optional[str] = None,
    allow_destructive: bool = False,
    advisory_lock: bool = True,
    verify_checksums: bool = True,
) -> tuple[Migration, ...]:
    """Apply every pending migration, in ascending order. Returns what it applied.

    Each migration runs in **its own transaction** together with its
    ``schema_migrations`` row, so a failure leaves neither the objects nor the
    record of them. Migrations already applied are not re-run; an up-to-date
    database is a no-op returning ``()``.

    Args:
        reader_role: the role the grant migrations should give ``SELECT`` to. A
            library cannot know a deployment's role names — the homelab's
            ``ai_gateway_metrics_reader`` means nothing in a customer tenant — so
            this is configuration. Left unset, the grant migrations no-op with a
            notice and nothing else changes. Note that it takes effect only on the
            run that *applies* migrations 004 and 005: they are the migrations that
            issue the grants, and a migration already recorded as applied is never
            re-run. Configuring a reader role against an already-migrated database
            therefore grants nothing — see README ("Granting a reader role later")
            for the remedy.
        allow_destructive: permit a migration containing ``DROP``, ``TRUNCATE`` or
            ``DELETE FROM``. Per-call on purpose: the operator's review should be
            visible where the call is, not in a config file somebody set last year.
        advisory_lock: serialize concurrent runs with a session-level Postgres
            advisory lock. Two deploys racing otherwise both try to create the
            same objects. Pass ``False`` for a store that has no such lock — the
            caller is then responsible for not running two migrators at once, and
            gets a warning saying so rather than a silent downgrade.
        verify_checksums: see :func:`pending`.

    Raises:
        DestructiveMigrationError: a pending migration is destructive and
            ``allow_destructive`` is false. Raised **before** anything is applied,
            so a destructive migration late in the set cannot leave the earlier
            ones half-applied.
        UnknownAppliedVersionError, MigrationChecksumError: see :func:`pending`.
    """
    # An autocommit connection breaks this function in two distinct ways, both
    # silent, so it is refused rather than documented.
    #
    # FR-009: each migration's DDL and its `schema_migrations` row are supposed to
    # commit together. Under autocommit they commit separately, so a failure
    # between them leaves a migration applied and unrecorded — or recorded and not
    # applied — which is the precise disagreement the one-transaction rule exists
    # to make impossible.
    #
    # FR-030: `reader_role` is published with `set_config(..., is_local => true)`,
    # which scopes it to the current transaction. Under autocommit that
    # transaction is the `set_config` statement itself, so migrations 004 and 005
    # see no role, take their no-op branch, and are then recorded as applied and
    # never re-run. The grant is not delayed, it is lost permanently, and the only
    # recovery is the manual GRANT in the README.
    if getattr(connection, "autocommit", False):
        raise ValueError(
            "tokenweir.migrations.apply needs a connection that is not in "
            "autocommit mode: each migration must commit together with its "
            "schema_migrations row, and the reader-role grant is scoped to the "
            "applying transaction, so autocommit would silently lose it for good. "
            "Set connection.autocommit = False."
        )

    if not advisory_lock:
        _logger.warning(
            "tokenweir: applying migrations without an advisory lock; two "
            "migrators running at once against this database are not serialized"
        )

    applied: list[Migration] = []
    # The lock is taken before *anything* touches `schema_migrations`, because
    # creating that table is itself part of the race this lock exists to settle:
    # `CREATE TABLE IF NOT EXISTS` is not atomic against a concurrent creation, so
    # two callers meeting a fresh database both observe "absent" and one dies on a
    # duplicate-key error from the system catalog. `pg_advisory_lock` needs no
    # table of its own, so nothing stops it from going first.
    with _advisory_lock(connection, advisory_lock):
        # `advisory_lock=False` inward: the lock is already held, and taking it
        # in exactly one place per call is easier to read than relying on
        # Postgres reference-counting a re-entrant acquisition.
        #
        # On a fresh database this call creates `schema_migrations`; on a
        # contended one it is the re-read that makes waiting worthwhile, since
        # whoever went first may have applied everything already.
        outstanding = pending(
            connection, verify_checksums=verify_checksums, advisory_lock=False
        )
        if not outstanding:
            return ()

        if not allow_destructive:
            offenders = {
                migration.filename: found
                for migration in outstanding
                if (found := destructive_statements(migration.sql))
            }
            if offenders:
                detail = "; ".join(
                    f"{name} ({', '.join(sorted(set(w.upper() for w in words)))})"
                    for name, words in sorted(offenders.items())
                )
                raise DestructiveMigrationError(
                    f"refusing to apply destructive migration(s): {detail} — pass "
                    "allow_destructive=True to proceed, and say in the review why "
                    "the data loss is intended"
                )

        for migration in outstanding:
            try:
                with connection.cursor() as cursor:
                    if reader_role is not None:
                        # set_config(..., is_local => true) rather than SET LOCAL:
                        # SET takes no parameters, and building the statement by
                        # string formatting is how a role name becomes an
                        # injection. Local to this transaction, so it does not
                        # leak into the caller's session.
                        cursor.execute(
                            "SELECT set_config('tokenweir.reader_role', %s, true)",
                            (reader_role,),
                        )
                    cursor.execute(migration.sql)
                    cursor.execute(
                        f"INSERT INTO {SCHEMA_MIGRATIONS_TABLE} "
                        "(version, name, checksum) VALUES (%s, %s, %s)",
                        (migration.version, migration.name, migration.checksum),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            applied.append(migration)
            _logger.info("tokenweir: applied migration %s", migration.filename)

    return tuple(applied)


def status(
    connection: Any, *, verify_checksums: bool = False, advisory_lock: bool = True
) -> tuple[Sequence[Migration], Sequence[Migration]]:
    """Return ``(applied, pending)`` as migration objects.

    ``verify_checksums`` defaults to **False** here and True on :func:`apply`:
    reporting state is exactly when you want to see a database that has drifted,
    rather than be refused a description of it.

    One read of ``schema_migrations``, not two: both halves of the answer come from
    the same snapshot, so a concurrent migration cannot land between them and make
    the pair describe a database that never existed.

    ``advisory_lock`` is on here too, and not as a formality: on a fresh database
    it is this call that creates the state table, so a health check running
    ``status`` against a database a deploy is migrating would otherwise be enough
    to kill the deploy.
    """
    shipped = discover()
    by_version = {m.version: m for m in shipped}
    known = applied_versions(connection, advisory_lock=advisory_lock)
    _check_applied(known, by_version, verify_checksums=verify_checksums)
    done = tuple(by_version[v] for v in sorted(known) if v in by_version)
    outstanding = tuple(m for m in shipped if m.version not in known)
    return done, outstanding


def connect(dsn: str, **kwargs: Any) -> Any:
    """Open a psycopg connection. The only place this package imports a driver.

    Deliberately not used by anything else here: :func:`apply` and the rest take a
    connection, so a deployment that pools its own, or uses psycopg 2, or wraps
    the connection for tracing, is not forced through this function.

    Raises:
        ImportError: if psycopg is not installed, naming the extra to install
            rather than leaving a bare ``ModuleNotFoundError`` for a caller to
            interpret.
    """
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - exercised in a subprocess test
        raise ImportError(
            "tokenweir needs psycopg to open a connection itself; install it with "
            "`pip install 'tokenweir[postgres]'`, or pass an existing DB-API "
            "connection instead — nothing else in tokenweir.migrations or "
            "tokenweir.postgres requires a driver"
        ) from exc
    return psycopg.connect(dsn, **kwargs)


def _iter_versions(migrations: Iterable[Migration]) -> str:
    """Render a migration list for a human. Used by the CLI."""
    return ", ".join(m.filename for m in migrations) or "(none)"
