"""``python -m tokenweir.migrations`` — apply or inspect the schema.

The replacement for the AI Gateway's ``scripts/migrate.py``. It is deliberately
thin: everything it does is a call into :mod:`tokenweir.migrations`, so a
deployment that would rather drive the runner from its own code loses nothing by
not using this.

Exit codes: ``0`` success, ``1`` a refusal or a database error (reported as a
message, not a traceback — an operator reading a deploy log is not debugging this
library), ``2`` a usage error from argparse.

    python -m tokenweir.migrations status --dsn "$DSN"
    python -m tokenweir.migrations apply  --dsn "$DSN" --reader-role metrics_reader
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Optional, Sequence

from tokenweir.migrations import (
    MigrationError,
    _iter_versions,
    apply,
    connect,
    discover,
    status,
)

#: Checked when ``--dsn`` is absent, so a credential need not appear in a process
#: listing or a shell history. Named for this package rather than reusing
#: libpq's ``PGDATABASE``/``DATABASE_URL`` so that pointing the migrator somewhere
#: is always a deliberate act.
DSN_ENV_VAR = "TOKENWEIR_DSN"

#: Same reasoning for the reader role: a deployment sets it once in its
#: environment rather than remembering the flag on every invocation.
READER_ROLE_ENV_VAR = "TOKENWEIR_READER_ROLE"


def _common_options() -> argparse.ArgumentParser:
    """Options accepted on either side of the subcommand.

    Attached to the top-level parser *and* to each subparser, because
    ``... status --dsn X`` is the form anyone writes first and argparse would
    otherwise reject it for appearing after the subcommand.

    ``default=argparse.SUPPRESS`` is what makes both positions work at once: a
    subparser normally writes its own default over whatever the top-level parser
    already parsed, so ``--dsn X status`` would come out as ``dsn=None``.
    Suppressed, an option that was not given is simply absent from the namespace —
    hence the ``getattr`` lookups below.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--dsn",
        default=argparse.SUPPRESS,
        help=f"Postgres connection string (default: ${DSN_ENV_VAR}).",
    )
    common.add_argument(
        "--reader-role",
        default=argparse.SUPPRESS,
        help=(
            "role to grant SELECT to in the grant migrations (default: "
            f"${READER_ROLE_ENV_VAR}). Without one those migrations no-op."
        ),
    )
    common.add_argument(
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="log each migration as it is applied.",
    )
    return common


def _build_parser() -> argparse.ArgumentParser:
    common = _common_options()
    parser = argparse.ArgumentParser(
        prog="python -m tokenweir.migrations",
        description="Apply or inspect tokenweir's usage-metering schema.",
        parents=[common],
    )

    sub = parser.add_subparsers(dest="command", required=True)
    status_parser = sub.add_parser(
        "status", help="show applied and pending migrations.", parents=[common]
    )
    status_parser.add_argument(
        "--verify-checksums",
        action="store_true",
        help=(
            "also check that every applied migration still matches the shipped "
            "file, and fail if one does not. Off by default so that a drifted "
            "database can still be described; on, this is the only way to notice "
            "the drift from the command line."
        ),
    )

    apply_parser = sub.add_parser(
        "apply", help="apply every pending migration.", parents=[common]
    )
    apply_parser.add_argument(
        "--allow-destructive",
        action="store_true",
        help=(
            "permit a migration containing DROP/TRUNCATE/DELETE FROM. Refused by "
            "default; this flag is the operator review."
        ),
    )
    apply_parser.add_argument(
        "--no-advisory-lock",
        action="store_true",
        help=(
            "skip the advisory lock that serializes concurrent runs. Only for a "
            "store that has none; you are then responsible for not racing."
        ),
    )
    return parser


def _resolve(args: argparse.Namespace, name: str, env_var: str) -> Optional[str]:
    """The flag if given (in either position), else the environment, else None."""
    value = getattr(args, name, None)
    if value is not None:
        return value
    return os.environ.get(env_var) or None


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if getattr(args, "verbose", False) else logging.WARNING,
        format="%(message)s",
    )

    dsn = _resolve(args, "dsn", DSN_ENV_VAR)
    if not dsn:
        print(
            f"error: no connection string; pass --dsn or set ${DSN_ENV_VAR}",
            file=sys.stderr,
        )
        return 1

    try:
        shipped = discover()
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        connection = connect(dsn)
    except ImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Anything the driver raises on connect — unreachable host, bad password,
        # no such database. An operator wants the reason on one line, not a
        # traceback through psycopg.
        print(f"error: could not connect: {exc}", file=sys.stderr)
        return 1

    try:
        if args.command == "status":
            done, outstanding = status(
                connection, verify_checksums=args.verify_checksums
            )
            print(f"shipped:  {_iter_versions(shipped)}")
            print(f"applied:  {_iter_versions(done)}")
            print(f"pending:  {_iter_versions(outstanding)}")
            return 0

        applied = apply(
            connection,
            reader_role=_resolve(args, "reader_role", READER_ROLE_ENV_VAR),
            allow_destructive=args.allow_destructive,
            advisory_lock=not args.no_advisory_lock,
        )
        if applied:
            print(f"applied:  {_iter_versions(applied)}")
        else:
            print("applied:  (none) — already up to date")
        return 0
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            connection.close()
        except Exception:  # pragma: no cover - nothing useful to do
            pass


if __name__ == "__main__":
    raise SystemExit(main())
