import httpx
import logging
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from django.conf import settings
from .jwt_auth import get_auth_header
from core.exceptions import AIServiceUnavailable, AIServiceError

logger = logging.getLogger(__name__)

SINGLE_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0,  write=10.0, pool=5.0)
BULK_TIMEOUT   = httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=5.0)


def _get_client(timeout: httpx.Timeout) -> httpx.Client:
    return httpx.Client(
        base_url=settings.FASTAPI_SERVICE_URL,
        timeout=timeout,
        headers={
            **get_auth_header(),           # fresh JWT every time client is created
            "Content-Type": "application/json",
        },
    )


@retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
def generate_content(payload: dict) -> dict:
    try:
        with _get_client(SINGLE_TIMEOUT) as client:
            response = client.post("/api/v1/generate", json=payload)
            response.raise_for_status()
            return response.json()

    except httpx.ConnectError as e:
        logger.error(f"FastAPI unreachable: {e}")
        raise AIServiceUnavailable("AI service is currently unavailable") from e

    except httpx.TimeoutException as e:
        logger.error(f"FastAPI timeout: {e}")
        raise AIServiceUnavailable("AI service timed out") from e

    except httpx.HTTPStatusError as e:
        logger.error(f"FastAPI error {e.response.status_code}: {e.response.text}")
        raise AIServiceError(
            f"AI service returned {e.response.status_code}",
            status_code=e.response.status_code,
        ) from e


def generate_bulk(payloads: list[dict]) -> dict:
    try:
        with _get_client(BULK_TIMEOUT) as client:
            response = client.post(
                "/api/v1/generate/bulk",
                json={"items": payloads},
            )
            response.raise_for_status()
            return response.json()

    except httpx.ConnectError as e:
        logger.error(f"FastAPI unreachable during bulk: {e}")
        raise AIServiceUnavailable("AI service is currently unavailable") from e

    except httpx.TimeoutException as e:
        logger.error(f"FastAPI bulk timeout: {e}")
        raise AIServiceUnavailable("AI service timed out during bulk generation") from e

    except httpx.HTTPStatusError as e:
        logger.error(f"FastAPI bulk error {e.response.status_code}: {e.response.text}")
        raise AIServiceError(
            f"Bulk generation failed: {e.response.status_code}",
            status_code=e.response.status_code,
        ) from e


def health_check() -> bool:
    try:
        with _get_client(httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)) as client:
            r = client.get("/health")
            return r.status_code == 200
    except Exception:
        return False