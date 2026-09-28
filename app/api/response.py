from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Response:
    status_code: int
    body: dict
    headers: dict = field(default_factory=dict)


def iso_utc(ts: datetime | None) -> str | None:
    """RFC 3339 UTC, whole seconds, as in the design's examples (DECISIONS.md D19)."""
    if ts is None:
        return None
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
