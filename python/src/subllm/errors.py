"""subllm exception hierarchy. All errors subclass SubllmError."""
from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from subllm.webagent import TraceEvent


class SubllmError(Exception):
    """Base for every error raised by subllm."""


class QuotaError(SubllmError):
    """Subscription quota exhausted (codex 5h/weekly cap, claude credit/limit).

    The only default trigger for FallbackLLM.
    """


class ClientError(SubllmError):
    """Subprocess failed: missing binary, crash, auth missing/invalid, tmux failure."""


class OutputError(SubllmError):
    """Model output unusable: empty, invalid JSON, or schema-validation mismatch."""


class RegionError(SubllmError):
    """Preflight: public IP fails the region guard — outside allowed_regions, or
    inside blocked_regions, or the lookup failed under on_lookup_failure='block'.

    Hard stop; NOT a default fallback trigger — every subscription client is
    equally out-of-region.
    """


class IsolationError(SubllmError):
    """Web-agent mode: the trace holds an event outside web search/fetch, or a
    fetch of a non-public target (D-5). Carries the trace collected so far.

    Hard stop; NOT a fallback trigger — the other client is no safer.
    """

    def __init__(self, message: str, trace: Iterable[TraceEvent] = ()) -> None:
        super().__init__(message)
        from subllm.webagent import TraceEvent as _TraceEvent

        events = tuple(trace)
        bad = [e for e in events if not isinstance(e, _TraceEvent)]
        if bad:
            raise TypeError(
                f"IsolationError trace must hold TraceEvent, got {type(bad[0]).__name__}"
            )
        self.trace: tuple[TraceEvent, ...] = events
