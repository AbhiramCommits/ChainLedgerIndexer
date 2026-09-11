"""Opaque cursor codec for keyset pagination over (block_number, log_index)."""

import base64
import json

from fastapi import HTTPException


def encode_cursor(block_number: int, log_index: int) -> str:
    payload = json.dumps({"b": block_number, "l": log_index}).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[int, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        block_number = int(data["b"])
        log_index = int(data["l"])
    except Exception:
        raise HTTPException(status_code=422, detail="invalid cursor") from None
    if block_number < 0 or log_index < 0:
        raise HTTPException(status_code=422, detail="invalid cursor")
    return block_number, log_index
