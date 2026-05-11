from rest_framework.exceptions import APIException
from rest_framework import status


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