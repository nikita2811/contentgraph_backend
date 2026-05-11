import redis
from django.conf import settings

# Upstash — for refresh token storage
upstash_client = redis.from_url(
    settings.UPSTASH_REDIS_URL,
    decode_responses=True,   # returns str instead of bytes
    socket_timeout=5,
    socket_connect_timeout=5,
)

# Local Redis — for Celery (no direct usage needed, Celery handles it)
# But if you need a direct local client anywhere:
local_redis_client = redis.from_url(
    settings.CELERY_BROKER_URL,
    decode_responses=True,
)