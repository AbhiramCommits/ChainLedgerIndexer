import pytest
from fastapi import HTTPException

from chainledger.api.pagination import decode_cursor, encode_cursor


def test_cursor_roundtrip():
    for block, log_idx in [(0, 0), (12345, 6), (10**9, 999)]:
        assert decode_cursor(encode_cursor(block, log_idx)) == (block, log_idx)


@pytest.mark.parametrize("bad", ["", "!!!", "not-base64!!", "AAAA", "e30="])
def test_cursor_rejects_garbage(bad):
    with pytest.raises(HTTPException) as exc:
        decode_cursor(bad)
    assert exc.value.status_code == 422


def test_cursor_rejects_negative_values():
    with pytest.raises(HTTPException) as exc:
        decode_cursor(encode_cursor(-1, 0))
    assert exc.value.status_code == 422
