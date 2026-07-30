import base64
import hashlib

import rfc8785
from pydantic import BaseModel


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def canonicalize_model(value: BaseModel) -> str:
    raw = value.model_dump(by_alias=True, exclude_none=True, mode="json")
    return rfc8785.dumps(raw).decode("utf-8")


def sha256_hex(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def compute_payload_hash(canonical_json: str) -> str:
    return sha256_hex(canonical_json).lower()


def compute_attestation_request_hash(nonce: str, payload_hash: str) -> str:
    # Must remain byte-for-byte compatible with `${nonce}${payloadHash}` in the TS client.
    return b64url(hashlib.sha256(f"{nonce}{payload_hash}".encode()).digest())
