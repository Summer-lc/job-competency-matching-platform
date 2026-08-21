from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Mapping


def observation_datetime(value: object, *, fallback: object = None) -> datetime:
    candidate = value if value not in (None, "") else fallback
    if isinstance(candidate, datetime):
        return (
            candidate.astimezone(timezone.utc).replace(tzinfo=None)
            if candidate.tzinfo is not None
            else candidate
        )
    if isinstance(candidate, str) and candidate.strip():
        try:
            parsed = datetime.fromisoformat(
                candidate.strip().replace("Z", "+00:00")
            )
        except ValueError:
            return datetime.min
        return (
            parsed.astimezone(timezone.utc).replace(tzinfo=None)
            if parsed.tzinfo is not None
            else parsed
        )
    return datetime.min


def observation_time(
    values: Mapping[str, object], *, fallback: object = None
) -> datetime:
    return observation_datetime(
        values.get("last_seen_at") or values.get("collected_at"),
        fallback=fallback,
    )


def observation_identity(
    values: Mapping[str, object], observation_at: datetime
) -> str:
    identity = {
        "observation_at": observation_at.isoformat(timespec="microseconds"),
        "source_id": values.get("source_id") or values.get("source_name"),
        "source_record_id": values.get("source_record_id") or values.get("record_id"),
    }
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_observation_payload(values: Mapping[str, object]) -> str:
    return json.dumps(
        dict(values),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value),
    )


__all__ = [
    "canonical_observation_payload",
    "observation_datetime",
    "observation_identity",
    "observation_time",
]
