import logging
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from contentgraph_backend.exceptions import AIServiceUnavailable, AIServiceError
from services.fastapi_client import generate_content
from .models import Product, CeleryTaskMeta, AIResult
from django.utils.timezone import now
import json

logger = logging.getLogger(__name__)


def _mark_failed(seo_request, meta, error_message, retries=0):
    if seo_request is not None:
        seo_request.status = 'failed'
        seo_request.save(update_fields=['status'])
    meta.status = 'failure'
    meta.error_message = error_message
    meta.retry_count = retries
    meta.completed_at = now()
    meta.save(update_fields=['status', 'error_message', 'retry_count', 'completed_at'])


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=5,
    soft_time_limit=270,
    time_limit=300,
    acks_late=True,
)
def generate_content_task(self, product_request_id) -> dict:
    """
    Calls FastAPI pipeline. Retries on transient errors only.
    Result is auto-stored in Redis by Celery under the task_id.
    """
    meta = CeleryTaskMeta.objects.get(task_id=self.request.id)

    # Mark started
    meta.status = 'started'
    meta.started_at = now()
    meta.save(update_fields=['status', 'started_at'])

    # Initialize data to None so except blocks can safely reference it
    # even if Product.objects.get() fails before assigning it.
    data = None

    try:
        data = Product.objects.get(id=product_request_id)
        logger.info(f"product data: {data}")
        product_details = {
            "product_name": data.product_name,   # fixed typo: "prodcut_name" → "product_name"
            "category": data.category,
            "target_audience": data.target_audience,
            "tone": data.tone,
            "key_features": data.key_features,
        }

        result = generate_content(product_details)
        response = result.result
        final_content_str = response["final_content"][0]["text"]
        serp_str = response["serp"][0]["text"]
            
            # Parse the nested JSON strings
        content = json.loads(final_content_str)
        serp = json.loads(serp_str)

        # Uncomment when ready to persist results:
        AIResult.objects.create(
            request=data,
            seo_title=content["seo_title"],
            meta_description=content["meta_description"],
            long_description=content["long_description"],
            tags=content["tags"],
            primary_keyword=serp["primary_keyword"],
            secondary_keyword=serp["secondary_keyword"],
        )

        # Mark request completed
        data.status = 'completed'
        data.save(update_fields=['status'])

        # Mark task success
        meta.status = 'success'
        meta.completed_at = now()
        meta.save(update_fields=['status', 'completed_at'])

        return result

    except AIServiceUnavailable as e:
        # FastAPI unreachable or timed out — retry with backoff
        if self.request.retries >= self.max_retries: # final retry exhausted
            logger.warning(f"[task={self.request.id}] Retries exhausted: {e}")
            raise
        logger.warning(f"[task={self.request.id}] Transient error, retrying: {e}")
        raise self.retry(exc=e, countdown=2 ** self.request.retries)

    except AIServiceError as e:
        # 4xx/5xx from FastAPI — don't retry, fail immediately
        logger.error(f"[task={self.request.id}] Non-retryable error: {e}")
        raise

    except SoftTimeLimitExceeded:
        logger.error(f"[task={self.request.id}] Pipeline exceeded 270s soft limit")
        raise AIServiceUnavailable("AI pipeline timed out")

    except Exception as e:
        logger.exception(f"[task={self.request.id}] Unexpected error: {e}")
        raise