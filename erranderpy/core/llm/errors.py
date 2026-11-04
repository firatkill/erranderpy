"""Map SDK exceptions to our provider-neutral `LLMError` kinds.

The `openai` and `anthropic` SDKs happen to use the same exception class
names, so one function handles both. Callers (and the on-call agent later)
only need to know the *kind*: is retrying pointless (auth, bad_request) or
might it work later (rate_limit, timeout, server)?
"""

from __future__ import annotations

from types import ModuleType

from erranderpy.core.llm.base import LLMError


def to_llm_error(exc: Exception, sdk: ModuleType, provider: str) -> LLMError:
    # Order matters: APITimeoutError is a subclass of APIConnectionError.
    if isinstance(exc, (sdk.AuthenticationError, sdk.PermissionDeniedError)):
        kind = "auth"
    elif isinstance(exc, sdk.RateLimitError):
        kind = "rate_limit"
    elif isinstance(exc, sdk.APITimeoutError):
        kind = "timeout"
    elif isinstance(exc, sdk.APIConnectionError):
        kind = "connection"
    elif isinstance(exc, sdk.InternalServerError):
        kind = "server"
    elif isinstance(exc, (sdk.BadRequestError, sdk.NotFoundError)):
        kind = "bad_request"
    elif isinstance(exc, sdk.APIStatusError):
        kind = "server" if getattr(exc, "status_code", 0) >= 500 else "bad_request"
    else:
        raise exc  # not an SDK error: a bug in our code, let it surface
    return LLMError(kind, str(exc), provider)
