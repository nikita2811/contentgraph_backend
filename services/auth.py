# services/jwt_auth.py
import jwt
from datetime import datetime, timedelta, timezone
from django.conf import settings
from functools import lru_cache

@lru_cache(maxsize=1)
def _get_private_key():
    """Cache parsed key — avoid re-parsing PEM on every request."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    key_bytes = settings.SERVICE_JWT_PRIVATE_KEY.encode()
    return load_pem_private_key(key_bytes, password=None)

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