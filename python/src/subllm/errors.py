"""subllm exception hierarchy. All errors subclass SubllmError."""


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
