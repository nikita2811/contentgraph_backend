import logging
import json
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from contentgraph_backend.exceptions import AIServiceUnavailable, AIServiceError
from services.fastapi_client import generate_content
from .models import Product, CeleryTaskMeta, AIResult,TokenUsage
from django.utils.timezone import now
import os

logger = logging.getLogger(__name__)

logger.info(f"DB URL in worker: {os.environ.get('DATABASE_URL', 'NOT SET')}")
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
    queue='default'
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
        if data.status == 'completed':
         return {"status": "already_completed", "product_id": data.id}

        

        product_details = {
            "product_name": data.product_name,
            "category": data.category,
            "target_audience": data.target_audience,
            "tone": data.tone,
            "key_features": data.key_features,
        }
        start = time.time()
        response = generate_content(product_details)
        elapsed_ms = int((time.time() - start) * 1000)
        # Parse nested JSON strings
        final_content_str = response["final_content"]
        content = json.loads(final_content_str)
        serp_raw = response["serp"]
        if isinstance(serp_raw, str):
            serp_list = json.loads(serp_raw)
            # serp is a list of blocks, find the text block
            serp_text = next(
                (block["text"] for block in serp_list if block.get("type") == "text"), 
                "{}"
            )
            # strip markdown code fences if present
            serp_text = serp_text.strip().removeprefix("```json").removesuffix("```").strip()
            serp = json.loads(serp_text)
        else:
            serp = serp_raw
                
        token_usage = response['token_usage']

        # Persist results
        AIResult.objects.get_or_create(
            request=data,
            defaults={
            'request'  : data,
            'seo_title' :content["seo_title"],
            'meta_description':content["meta_description"],
            'meta_title':content["h1"],
            'long_description':content["intro_paragraph"],
            'generation_time_ms': elapsed_ms,
            # join if tags is CharField, remove join if JSONField
            "tags":content["tags"] if isinstance(content["tags"], list) else content["tags"].split(","),  # ← pass list directly
            'primary_keyword':serp["primary_keyword"],
            # join if secondary_keywords is a list and field is CharField
            "secondary_keyword": ",".join(serp["secondary_keywords"]) if isinstance(serp["secondary_keywords"], list) else serp["secondary_keywords"],  # ← now TextField, no truncation needed
            }
        )
        
        TokenUsage.objects.create(
            product_name=product_details.get("product_name", ""),
            prompt_tokens=token_usage.get("prompt_tokens", 0),      # ← dict access
            completion_tokens=token_usage.get("completion_tokens", 0),
            total_tokens=token_usage.get("total_tokens", 0),
            model_name=token_usage.get("model_name", ""),
            task_id=self.request.id,
        )
        # Mark request completed
        data.status = 'completed'
        data.save(update_fields=['status'])

        # Mark task success
        meta.status = 'success'
        meta.completed_at = now()
        meta.save(update_fields=['status', 'completed_at'])

        # Return serializable dict
        return {"status": "success", "product_id": data.id}

    except AIServiceUnavailable as e:
        if self.request.retries >= self.max_retries:
            _mark_failed(data, meta, str(e), self.request.retries)
            logger.warning(f"[task={self.request.id}] Retries exhausted: {e}")
            raise
        logger.warning(f"[task={self.request.id}] Transient error, retrying: {e}")
        raise self.retry(exc=e, countdown=2 ** self.request.retries)

    except AIServiceError as e:
        _mark_failed(data, meta, str(e), self.request.retries)
        logger.error(f"[task={self.request.id}] Non-retryable error: {e}")
        raise

    except SoftTimeLimitExceeded:
        _mark_failed(data, meta, "AI pipeline timed out", self.request.retries)
        logger.error(f"[task={self.request.id}] Pipeline exceeded 270s soft limit")
        raise AIServiceUnavailable("AI pipeline timed out")

    except Exception as e:
        _mark_failed(data, meta, str(e), self.request.retries)
        logger.exception(f"[task={self.request.id}] Unexpected error: {e}")
        raise