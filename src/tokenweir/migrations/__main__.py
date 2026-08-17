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
import re
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

#: libpq's own connection keywords, plus the URL schemes. These are the only
#: pieces of a DSN safe to leave in a message, because they are the same for
#: everyone; anything else in a connection string is somebody's configuration.
_SAFE_DSN_WORDS = frozenset(
    """
    postgres postgresql host hostaddr port dbname user password passfile
    channel_binding connect_timeout client_encoding options application_name
    fallback_application_name keepalives keepalives_idle keepalives_interval
    keepalives_count tcp_user_timeout replication gssencmode sslmode sslcompression
    sslcert sslkey sslpassword sslrootcert sslcrl sslcrldir sslsni requirepeer
    ssl_min_protocol_version ssl_max_protocol_version krbsrvname gsslib service
    target_session_attrs load_balance_hosts require_auth
    """.split()
)

#: Where a DSN's words end: whitespace and the URL/keyword punctuation.
_DSN_SPLIT_RE = re.compile(r"[\s=:@/?&]+")


def _carries_a_password(dsn: str) -> bool:
    """Whether this DSN could contain a secret at all.

    Two shapes: the ``password=`` keyword, and a URL whose userinfo has a colon
    (``postgresql://user:secret@host/db``). A URL with a bare user and no colon
    carries nothing to hide.
    """
    if re.search(r"(?<![a-z_])password\s*=", dsn, re.IGNORECASE):
        return True
    userinfo = re.match(r"[a-z+]+://([^/@?]*)@", dsn, re.IGNORECASE)
    return bool(userinfo and ":" in userinfo.group(1))


def _redact(message: str, dsn: str) -> str:
    """Mask anything from ``dsn`` that shows up in a driver's error ``message``.

    The naive version of this — mask the password value — does not work, because
    the case that leaks is the one where the DSN could not be *parsed*, and an
    unparsable DSN has no reliable password value to extract. A password with an
    unquoted space is an ordinary typo::

        --dsn "host=h password=p4ss w0rd dbname=d"
        libpq: missing "=" after "w0rd" in connection info string

    libpq quotes the token it choked on, and that token is half the password. So
    the rule here is the conservative one: every word of the DSN that is not a
    libpq keyword is treated as configuration and masked wherever it appears.

    That deliberately over-masks — a hostname goes too, and a password that
    happens to be a common English word will blank that word out of the message.
    Both are the acceptable direction: this text is printed to a deploy log
    somebody else reads, and what an operator needs from it ("connection
    refused", "missing = after") survives masking intact.
    """
    if not _carries_a_password(dsn):
        return message

    words = {
        word
        for word in _DSN_SPLIT_RE.split(dsn)
        # Under three characters is left alone: too short to be worth hiding, and
        # long enough to be a fragment of an unrelated word in the message.
        if len(word) >= 3 and word.lower() not in _SAFE_DSN_WORDS
    }
    for word in sorted(words, key=len, reverse=True):
        message = message.replace(word, "***")
    return message


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
        # traceback through psycopg. Redacted first: libpq quotes the token it
        # choked on, and for a malformed DSN that token can be part of the
        # password. This goes to a deploy log somebody else reads.
        print(f"error: could not connect: {_redact(str(exc), dsn)}", file=sys.stderr)
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
