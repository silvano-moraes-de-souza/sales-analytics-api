"""Opaque keyset cursors.

A cursor is the sort key of the last row of a page, JSON-encoded and base64'd.
The client treats it as a token; the server resumes right after that key, so a
page costs the same wherever it is in the listing.
"""

from __future__ import annotations

import base64
import binascii
import json


class BadCursorError(ValueError):
    pass


def encode(values: list) -> str:
    raw = json.dumps(values, separators=(",", ":"), default=str).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(cursor: str, size: int) -> list:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        values = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise BadCursorError("cursor is not valid") from exc
    if not isinstance(values, list) or len(values) != size:
        raise BadCursorError("cursor is not valid for this listing")
    return values
