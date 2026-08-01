import hashlib
import hmac
import json
import secrets


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
