# services/jwt_auth.py
import jwt
from datetime import datetime, timedelta, timezone
from django.conf import settings
from functools import lru_cache
from dotenv import load_dotenv
import base64
from cryptography.hazmat.primitives.serialization import (
    load_pem_private_key,
  
)


@lru_cache(maxsize=1)
def _get_private_key():
    """Cache parsed key — avoid re-parsing PEM on every request."""

    raw = settings.SERVICE_JWT_PRIVATE_KEY

    # Strip any accidental whitespace, quotes, or CRLF Windows added
    raw = raw.strip().strip('"').strip("'")

    try:
        pem_bytes = base64.b64decode(raw)           # decode base64 → raw PEM bytes
    except Exception as e:
        raise ValueError(f"Base64 decode failed: {e}\nRaw value start: {raw[:50]}")
    
    try:
        return load_pem_private_key(pem_bytes, password=None)
    except Exception as e:
        # Print decoded PEM to help debug framing issues
        raise ValueError(f"PEM load failed: {e}\nDecoded PEM:\n{pem_bytes.decode()}")

   

def generate_service_token() -> str:
    """
    Generate a short-lived JWT signed with RS256.
    Call this fresh on every outgoing HTTP request — tokens expire in 5 min.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "iss": "django-service",          # issuer identity
        "aud": "fastapi-service",         # FastAPI will reject tokens with wrong aud
        "sub": "service-account",         # subject — useful for logging
        "iat": now,                       # issued at
        "exp": now + timedelta(minutes=5),# short expiry — leaked token useless fast
        "service": "content-generator",  # custom claim for extra context
    }
    return jwt.encode(payload, _get_private_key(), algorithm="RS256")


def get_auth_header() -> dict:
    """Drop-in header dict for httpx requests."""
    return {"Authorization": f"Bearer {generate_service_token()}"}