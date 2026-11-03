"""Fixed UUIDv7/Crockford identity shared by output, snapshots and results."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Self

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    uuid: uuid.UUID
    run_id: str
    utc_timestamp: str
    utc_datetime: str

    @classmethod
    def create(cls) -> Self:
        value = uuid.uuid7()
        integer = value.int
        digits = []
        for _ in range(26):
            digits.append(_ALPHABET[integer & 31])
            integer >>= 5
        # Integer epoch arithmetic preserves millisecond boundaries exactly.
        instant = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value.time)
        return cls(
            value,
            "".join(reversed(digits)),
            instant.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            instant.strftime("%Y%m%dT%H%M%S") + f"{value.time % 1000:03d}Z",
        )
