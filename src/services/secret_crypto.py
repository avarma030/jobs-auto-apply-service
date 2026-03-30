from __future__ import annotations

import base64
import json
from functools import lru_cache
from hashlib import sha256

from cryptography.fernet import Fernet

from src.config import settings


def _normalize_fernet_key(raw_key: str) -> bytes:
    candidate = raw_key.strip().encode("utf-8")
    try:
        Fernet(candidate)
        return candidate
    except Exception:
        pass
    digest = sha256(raw_key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


@lru_cache(maxsize=1)
def get_secret_fernet() -> Fernet:
    raw_key = settings.data_encryption_key or settings.secret_key
    return Fernet(_normalize_fernet_key(raw_key))


def encrypt_secret_payload(payload: dict[str, str]) -> str:
    compact = {key: value for key, value in payload.items() if value}
    token = get_secret_fernet().encrypt(
        json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    return token.decode("utf-8")


def decrypt_secret_payload(token: str | None) -> dict[str, str]:
    if not token:
        return {}
    raw = get_secret_fernet().decrypt(token.encode("utf-8"))
    data = json.loads(raw.decode("utf-8"))
    return {
        str(key): str(value)
        for key, value in data.items()
        if value is not None and str(value).strip()
    }
