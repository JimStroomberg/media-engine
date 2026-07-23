from __future__ import annotations

import contextvars
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass
class ProviderUsageEvent:
    event_id: str
    provider: str
    model: str
    operation: str
    outcome: str
    usage: dict[str, Any]
    latency_ms: int
    started_at: str
    completed_at: str

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


_usage_events: contextvars.ContextVar[list[ProviderUsageEvent] | None] = contextvars.ContextVar(
    "provider_usage_events",
    default=None,
)


@contextmanager
def capture_provider_usage() -> Iterator[list[ProviderUsageEvent]]:
    events: list[ProviderUsageEvent] = []
    token = _usage_events.set(events)
    try:
        yield events
    finally:
        _usage_events.reset(token)


def record_provider_usage(
    *,
    provider: str,
    model: str,
    operation: str,
    usage: dict[str, Any],
    started_at: datetime,
    completed_at: datetime | None = None,
) -> None:
    events = _usage_events.get()
    if events is None:
        return
    completed = completed_at or datetime.now(UTC)
    latency_ms = max(0, round((completed - started_at).total_seconds() * 1000))
    events.append(
        ProviderUsageEvent(
            event_id=str(uuid.uuid4()),
            provider=provider,
            model=model,
            operation=operation,
            outcome="response_received",
            usage=usage,
            latency_ms=latency_ms,
            started_at=started_at.isoformat(),
            completed_at=completed.isoformat(),
        )
    )


def mark_usage_outcome(events: list[ProviderUsageEvent], outcome: str) -> None:
    for event in events:
        event.outcome = outcome
