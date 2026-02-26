from __future__ import annotations

import base64
import hashlib


def encrypt_token(token: str, key: str) -> str:
    raw = token.encode("utf-8")
    return base64.urlsafe_b64encode(_xor_with_key(raw, key)).decode("ascii")


def decrypt_token(ciphertext: str, key: str) -> str:
    raw = base64.urlsafe_b64decode(ciphertext.encode("ascii"))
    plain = _xor_with_key(raw, key)
    return plain.decode("utf-8")


def _xor_with_key(raw: bytes, key: str) -> bytes:
    if not key:
        raise ValueError("key must not be empty")
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return bytes(value ^ digest[idx % len(digest)] for idx, value in enumerate(raw))
