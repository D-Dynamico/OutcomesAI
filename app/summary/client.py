# The generate_summary interface (brief: "Summary service contract"; design section 5).
#
# Every provider failure surfaces as TransientError with a fixed error_class. The raw
# provider error is never kept, since it may echo transcript content (section 4, section 7).
import asyncio

import httpx

# Fixed enum (section 4). 'worker_lost' is recorded by the claim, never raised here.
ERROR_CLASSES = ("ai_timeout", "rate_limited", "ai_unavailable", "worker_lost", "internal")

# The circuit breaker's probe input (section 5): short, fixed, no patient data.
# mock-ai recognises it to count probe calls separately from real ones.
PROBE_TRANSCRIPT = "Nurse: This is a connectivity check. Patient: Understood."


class TransientError(Exception):
    """A failed generate_summary call. Carries only the error class, never provider text."""

    def __init__(self, error_class: str):
        if error_class not in ERROR_CLASSES:
            raise ValueError("unknown error_class")
        super().__init__(error_class)
        self.error_class = error_class


class SummaryClient:
    def generate_summary(self, transcription: str) -> str:
        raise NotImplementedError


class HttpSummaryClient(SummaryClient):
    """Calls the provider over HTTP with the worker's own timeout (section 5, 30 seconds).

    The whole call runs under a total deadline of timeout_seconds (asyncio.timeout), on top of
    httpx's per-phase timeouts. Cancelling at the deadline drops the connection wherever the
    request is, so a provider trickling its response cannot outlast it. The lease > AI timeout
    rule depends on this bound (DECISIONS.md D21).
    """

    STATUS_ERRORS = {429: "rate_limited", 503: "ai_unavailable", 504: "ai_timeout"}

    def __init__(self, base_url: str, timeout_seconds: float, transport: httpx.AsyncBaseTransport | None = None):
        self._base_url = base_url
        self._timeout = timeout_seconds
        self._transport = transport

    def close(self) -> None:
        pass   # one client per call; nothing held between calls

    async def _post(self, transcription: str) -> httpx.Response:
        async with asyncio.timeout(self._timeout):
            async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout,
                                         transport=self._transport) as client:
                return await client.post("/generate_summary", json={"transcription": transcription})

    def generate_summary(self, transcription: str) -> str:
        try:
            # Worker loops are plain threads with no running event loop
            response = asyncio.run(self._post(transcription))
        except (httpx.TimeoutException, TimeoutError):
            raise TransientError("ai_timeout") from None
        except httpx.TransportError:
            raise TransientError("ai_unavailable") from None
        except Exception:
            # DECISIONS.md D5: unexpected exceptions raised by the provider call itself
            raise TransientError("internal") from None

        if response.status_code in self.STATUS_ERRORS:
            raise TransientError(self.STATUS_ERRORS[response.status_code])
        if response.status_code != 200:
            raise TransientError("ai_unavailable" if response.status_code >= 500 else "internal")
        try:
            summary = response.json()["summary"]
        except Exception:
            raise TransientError("internal") from None
        if not isinstance(summary, str):
            raise TransientError("internal")
        return summary
