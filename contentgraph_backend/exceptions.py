# contentgraph_backend/exceptions.py

from rest_framework.exceptions import APIException
from rest_framework import status


# ── DRF exceptions for views/API responses ──
class AIServiceUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_code = "ai_service_unavailable"

    def __init__(self, detail=None):
        super().__init__(detail or "AI service is currently unavailable")


class AIServiceError(APIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_code = "ai_service_error"

    def __init__(self, detail=None, status_code=None):
        if status_code:
            self.status_code = status_code
        super().__init__(detail or "AI service returned an error")


# ── Plain exceptions for internal use in Celery tasks ──
class AIServiceUnavailableError(Exception):
    """Raised inside Celery tasks when FastAPI is unreachable. Retryable."""
    default_code = "ai_service_unavailable"

    def __init__(self, detail=None, status_code=None):   # ← detail FIRST now
        self.detail = detail or "AI service is currently unavailable"
        self.status_code = status_code or 503
        super().__init__(self.detail)


class AIServiceFailedError(Exception):
    """Raised inside Celery tasks when FastAPI returns an error. Non-retryable."""
    default_code = "ai_service_failed"

    def __init__(self, detail=None, status_code=None):   # ← detail FIRST now
        self.detail = detail or "AI service returned an error"
        self.status_code = status_code or 500
        super().__init__(self.detail)