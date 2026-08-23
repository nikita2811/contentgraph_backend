import logging
import json
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from contentgraph_backend.exceptions import AIServiceFailedError,AIServiceUnavailableError
from services.fastapi_client import generate_content
from .models import Product, CeleryTaskMeta, AIResult, TokenUsage
from django.utils.timezone import now
import os
from payment.services.billing_service import BillingService
import time
from django.contrib.auth import get_user_model
from payment.services.wallet_service import InsufficientBalanceError



logger = logging.getLogger(__name__)
User = get_user_model()

logger.info(f"DB URL in worker: {os.environ.get('DATABASE_URL', 'NOT SET')}")




def _mark_failed(product, charge, meta, error_message, retries=0):
    if product is not None:
        product.status = 'failed'
        product.save(update_fields=['status'])
    meta.status = 'failure'
    meta.error_message = error_message
    meta.retry_count = retries
    meta.completed_at = now()
    meta.save(update_fields=['status', 'error_message', 'retry_count', 'completed_at'])

    if charge is not None:  # ← only refund if charge succeeded
        BillingService.refund_failed_job(charge=charge, product_request=product)


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=5,
    soft_time_limit=270,
    time_limit=300,
    acks_late=True,
    queue='default'
)
def generate_content_task(self, product_request_id: int, user_id: int, is_regenerate: bool = False) -> dict:
    user = User.objects.get(id=user_id)
    charge = None  # ← initialize before try so except blocks can safely reference it

    
  

    product = None
    meta = None   # ← initialize so _mark_failed is safe if .get() throws

    try:
          # fetch meta by request_id to avoid race condition with task_id
        meta = CeleryTaskMeta.objects.get(request_id=product_request_id)
        print(f"meta type: {type(meta)}, value: {meta}")
        meta.status = 'started'
        meta.started_at = now()
        meta.save(update_fields=['status', 'started_at'])
        product = Product.objects.get(id=product_request_id)

        if product.status == 'completed':
            return {"status": "already_completed", "product_id": product.id}
        
        charge = None
        
        if not BillingService.already_charged(product_request=product):
           charge = BillingService.charge_for_usage(  # ← inside try, after product fetch
               user=user,
               units=1,
               product_request=product,
           )
        else:
          charge = BillingService.get_charge(product_request=product)

        product_details = {
            "product_name": product.product_name,
            "category": product.category,
            "target_audience": product.target_audience,
            "tone": product.tone,
            "key_features": product.key_features,
            "regenerate": is_regenerate,
        }

        start = time.time()
        response = generate_content(product_details)
        elapsed_ms = int((time.time() - start) * 1000)

      
       
        
        # parse token_usage
        token_usage_raw = response.get("token_usage", {})
        
        if isinstance(token_usage_raw, str):
            token_usage = json.loads(token_usage_raw)
        elif isinstance(token_usage_raw, list):
            # content block list — extract text block and parse
            token_usage_text = next(
                (block["text"] for block in token_usage_raw if block.get("type") == "text"),
                None
            )
            token_usage = json.loads(token_usage_text) if token_usage_text else {}
        else:
            # already a dict (normal case now)
            token_usage = token_usage_raw or {}
        
        print(f"token_usage: {token_usage}")


              
     
        
        AIResult.objects.update_or_create(
            request=product,
            defaults={  # ← 'request' removed from defaults, it's the lookup key
            'seo_title': response['seo_title'],
            'meta_description': response['meta_description'],
            'meta_title': response['meta_title'],
            'long_description': response['intro_paragraph'],
            'generation_time_ms': elapsed_ms,
            'tags': response['tags'] if isinstance(response['tags'], list) else response['tags'].split(","),
            'primary_keyword': response['primary_keyword'],
            'secondary_keyword': ",".join(response['secondary_keyword']) if isinstance(response['secondary_keyword'], list) else response['secondary_keyword'],
                       }
        )

        TokenUsage.objects.create(
            product_name=product_details.get("product_name", ""),
            prompt_tokens=token_usage.get("prompt_tokens", 0),
            completion_tokens=token_usage.get("completion_tokens", 0),
            total_tokens=token_usage.get("total_tokens", 0),
            model_name=token_usage.get("model_name", ""),
            task_id=self.request.id,
        )

        product.status = 'completed'
        product.save(update_fields=['status'])

        meta.status = 'success'
        meta.completed_at = now()
        meta.save(update_fields=['status', 'completed_at'])

        return {"status": "success", "product_id": product.id}

    except AIServiceUnavailableError as e:          # ← fixed to match fastapi_client.py
        if self.request.retries >= self.max_retries:
            _mark_failed(product, charge, meta, str(e), self.request.retries)
            logger.exception(f"[task={self.request.id}] Retries exhausted")   # ← full traceback
            raise
        logger.warning(f"[task={self.request.id}] Transient error, retrying: {e}")
        raise self.retry(exc=e, countdown=2 ** self.request.retries)

    except AIServiceFailedError as e:                # ← fixed to match fastapi_client.py
        _mark_failed(product, charge, meta, str(e), self.request.retries)
        logger.exception(f"[task={self.request.id}] Non-retryable error")     # ← full traceback
        raise
    except SoftTimeLimitExceeded:
        _mark_failed(product, charge, meta, "AI pipeline timed out", self.request.retries)
        logger.exception(f"[task={self.request.id}] Pipeline exceeded 270s soft limit")  # ← full traceback
        raise AIServiceUnavailableError(detail="AI pipeline timed out")       # ← keyword, unambiguous

    except Exception as e:
        _mark_failed(product, charge, meta, str(e), self.request.retries)
        logger.exception(f"[task={self.request.id}] Unexpected error")        # already correct
        raise