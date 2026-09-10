"""token-weir — provider-neutral, transport-agnostic usage & cost metering.

Extraction target for TOKWEIR-1 (see docs ADR-0001). This package defines the
stable seams the gateway's usage pipeline is being pulled into:

- ``contract``  — the versioned, serializable usage-record contract
- ``sink``      — the emit-side interface (fire-and-forget, off the critical path),
  plus the guarded seam (``emit_usage`` and its halves) that keeps record
  construction off a metered request's critical path too, the optional
  ``BatchSink`` capability, and ``DirectSink`` — the broker-less path
- ``emitter``   — ``BufferedEmitter``, the client that actually *discharges* the
  fire-and-forget contract: buffers, returns immediately, swallows failures
- ``source``    — the write-side interface (persists records to a store)
- ``orchestrator`` — the MADO side of attribution: the phase taxonomy and the
  environment block an orchestrator injects for the Claude Code ``Stop`` hook to
  read back (TOKWEIR-8). Stdlib-only and hook-free, so a producer in another
  codebase can depend on the contract without depending on the capture path.

Transport adapters live behind optional extras and are reached by their own import
path, never from this namespace, so ``import tokenweir`` carries no wire
dependency: ``tokenweir.amqp`` (``tokenweir[amqp]``) is the homelab's broker path,
and ``DirectSink`` over a ``Source`` is the broker-less one.
"""

import logging

from tokenweir.contract import (
    REQUIRED_FIELDS,
    SCHEMA_VERSION,
    TOKEN_COUNT_FIELDS,
    PricingMode,
    UsageRecord,
    usage_record_json_schema,
)
from tokenweir.emitter import BufferedEmitter, EmitterStats
from tokenweir.orchestrator import (
    ATTRIBUTION_ENV,
    PhaseKind,
    attribution_env,
    is_canonical_phase,
    normalize_phase,
    phase_label,
)
from tokenweir.sink import (
    BatchSink,
    DirectSink,
    NullSink,
    Sink,
    build_record,
    emit_record,
    emit_usage,
)
from tokenweir.source import MemorySource, Source

# The stdlib idiom for a library: attach a NullHandler to the package logger so
# an application that has configured no logging is not written to. Without it,
# `logging.lastResort` prints every dropped-record WARNING — traceback and all —
# to the application's stderr, which on a hot metered path with a systematically
# broken producer is one traceback per request. That is the library taking an
# output decision that belongs to whoever embeds it.
#
# The trade-off, recorded rather than assumed: an application with no logging
# configuration now sees nothing. That is the correct default (it is the
# application's choice to make, and one line of `logging.basicConfig()` reverses
# it), and drops stay observable regardless of logging configuration through the
# return value, which is the signal a caller can act on programmatically.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "SCHEMA_VERSION",
    "REQUIRED_FIELDS",
    "TOKEN_COUNT_FIELDS",
    "UsageRecord",
    "PricingMode",
    "usage_record_json_schema",
    "Sink",
    "BatchSink",
    "NullSink",
    "DirectSink",
    "BufferedEmitter",
    "EmitterStats",
    "build_record",
    "emit_record",
    "emit_usage",
    "Source",
    "MemorySource",
    "ATTRIBUTION_ENV",
    "PhaseKind",
    "attribution_env",
    "is_canonical_phase",
    "normalize_phase",
    "phase_label",
]

__version__ = "0.0.0"
