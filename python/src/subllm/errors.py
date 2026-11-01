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


class HumanActionRequired(SubllmError):
    """Chat web route: a person must act (log in, solve a challenge) before any send."""

    def __init__(self, message: str, *, browser: str | None = None, detail: str | None = None) -> None:
        super().__init__(message)
        self.browser = browser
        self.detail = detail


class SendUnconfirmedError(SubllmError):
    """Chat web route: the send key was pressed but the outcome is unknown.

    Never retried or resent; the conversation may exist on the server.
    """

    def __init__(self, message: str, *, conversation_url: str | None = None) -> None:
        super().__init__(message)
        self.conversation_url = conversation_url


class ModelDowngradeError(SubllmError):
    """Chat web route: the served model is not the requested mode. The answer is kept."""

    def __init__(
        self, message: str, *, text: str, served_model: str | None, requested_mode: str | None
    ) -> None:
        super().__init__(message)
        self.text = text
        self.served_model = served_model
        self.requested_mode = requested_mode
