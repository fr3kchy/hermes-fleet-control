import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass


def random_token() -> str:
    return secrets.token_urlsafe(32)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def canonical_json(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def sign_payload(secret: str, payload: dict) -> str:
    return hmac.new(secret.encode(), canonical_json(payload), hashlib.sha256).hexdigest()


def verify_signature(secret: str, payload: dict, signature: str) -> bool:
    return hmac.compare_digest(sign_payload(secret, payload), signature)


def body_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def canonical_request(method: str, path: str, timestamp: str, nonce: str, body: bytes) -> bytes:
    return "\n".join((method.upper(), path, timestamp, nonce, body_sha256(body))).encode()


def sign_request(secret: str, method: str, path: str, timestamp: str, nonce: str, body: bytes = b"") -> str:
    return hmac.new(secret.encode(), canonical_request(method, path, timestamp, nonce, body), hashlib.sha256).hexdigest()


def verify_request_signature(secret: str, method: str, path: str, timestamp: str, nonce: str, body: bytes, signature: str) -> bool:
    return hmac.compare_digest(sign_request(secret, method, path, timestamp, nonce, body), signature)


def fresh_timestamp(value: str, max_age_seconds: int = 90, current_time: float | None = None) -> bool:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    current = time.time() if current_time is None else current_time
    return abs(current - parsed) <= max_age_seconds


@dataclass(frozen=True)
class SignedHeaders:
    timestamp: str
    nonce: str
    signature: str


def signed_headers(secret: str, method: str, path: str, body: bytes = b"", prefix: str = "Node") -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(18)
    return {
        f"X-{prefix}-Timestamp": timestamp,
        f"X-{prefix}-Nonce": nonce,
        f"X-{prefix}-Signature": sign_request(secret, method, path, timestamp, nonce, body),
    }
