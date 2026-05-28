import csv
import io
import boto3
from django.conf import settings
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from contentgraph_backend.exceptions import AIServiceUnavailable, AIServiceError
from services.fastapi_client import generate_content
from .models import Product, CeleryTaskMeta, AIResult,BulkJob,BulkJobItem,TokenUsage
import boto3
from django.utils.timezone import now
import logging
import json
import uuid
import time

logger = logging.getLogger(__name__)

s3 = boto3.client(
    's3',
    aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
    aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    region_name=settings.AWS_DEFAULT_REGION
)

def normalize_row(row):
    return {k.strip().lower().replace(' ', '_'): v.strip() if isinstance(v, str) else v 
            for k, v in row.items()}

def parse_key_features(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [f.strip() for f in value.split(',') if f.strip()]
    return []

def to_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [f.strip() for f in value.split(',') if f.strip()]
    return []

def to_str(value):
    if not value:
        return ""
    if isinstance(value, list):
        return ", ".join(value)
    return str(value)


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
    soft_time_limit=570,
    time_limit=600,
    acks_late=True,
    queue='csv'
)
def process_csv_task(self, bulk_job_id) -> dict:
    

    job = None

    try:
        job = BulkJob.objects.get(id=bulk_job_id)

        # Skip if already completed — idempotency check
        if job.status == 'completed':
            return {"status": "already_completed", "job_id": str(job.id)}

        job.status = 'processing'
        job.save(update_fields=['status'])

        # Read CSV from S3
        obj = s3.get_object(Bucket=settings.AWS_BUCKET_NAME, Key=job.s3_key)
        content = obj['Body'].read().decode('utf-8')
        reader = list(csv.DictReader(io.StringIO(content)))

        # Update total count
        job.total_items = len(reader)
        job.save(update_fields=['total_items'])

        output_rows = []

        for position, row in enumerate(reader):
            row = normalize_row(row)
           
            # Create Product per row
            product,_ = Product.objects.get_or_create(
                bulk_job_id=job,
                user=job.user,
                product_name=row.get('product_name', ''),
                defaults={
                    'product_name': row.get('product_name', ''),
                    'category': row.get('category', ''),
                    'target_audience': row.get('target_audience', ''),
                    'tone': row.get('tone', ''),
                    'key_features': row.get('key_features', ''),
                    'status': 'pending',
                    'request_type': 'bulk',
                }
            )

            # Create BulkJobItem per row
            bulk_item, _ = BulkJobItem.objects.get_or_create(
                bulk_job=job,
                request=product,
                defaults={
                    'position': position,
                    'row_index': position,
                    'status': 'processing',
                }
            )

            # Create CeleryTaskMeta per row
            
            task_id = str(uuid.uuid4())
            meta,_ = CeleryTaskMeta.objects.get_or_create(
                request=product,
                defaults={
                    'task_id': task_id,
                    'task_name': 'process_csv_row',
                    'queue_type': 'csv',
                    'status': 'started',
                    'started_at': now(),
                    'task_meta': bulk_item,
                }
            )

            try:
                product_details = {
                    "product_name": product.product_name,
                    "category": product.category,
                    "target_audience": product.target_audience,
                    "tone": product.tone,
                    "key_features": product.key_features if isinstance(product.key_features, list) else parse_key_features(product.key_features),
                }

                
                logger.warning(product_details)  # verify it looks correct
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
                    request=product,
                    defaults={
                    'request'  : product,
                    'seo_title' :content["seo_title"],
                    'meta_description':content["meta_description"],
                    'meta_title':content["h1"],
                    'generation_time_ms': elapsed_ms,
                    'long_description':content["intro_paragraph"],
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
        


                # Mark row success
                product.status = 'completed'
                product.save(update_fields=['status'])

                bulk_item.status = 'completed'
                bulk_item.save(update_fields=['status'])

                meta.status = 'success'
                meta.completed_at = now()
                meta.save(update_fields=['status', 'completed_at'])

                job.processed_items += 1
                job.save(update_fields=['processed_items'])

                output_rows.append({
                    'product_name': product.product_name,
                    'seo_title' :content["seo_title"],
                    'meta_title':content["h1"],
                    'meta_description':content["meta_description"],
                    'long_description':content["intro_paragraph"],
                    # join if tags is CharField, remove join if JSONField
                    'tags':",".join(content["tags"]) if isinstance(content["tags"], list) else content["tags"],
                })

            except Exception as row_error:
                # Don't fail entire job for one bad row
                logger.error(f"Row {position} failed: {row_error}")

                bulk_item.status = 'failed'
                bulk_item.error_message = str(row_error)
                bulk_item.save(update_fields=['status', 'error_message'])

                meta.status = 'failure'
                meta.error_message = str(row_error)
                meta.completed_at = now()
                meta.save(update_fields=['status', 'error_message', 'completed_at'])

                job.failed_items += 1
                job.save(update_fields=['failed_items'])

                continue  # move to next row

        # Write result CSV to S3
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=['product_name', 'seo_title','meta_title', 'meta_description', 'long_description','tags'])
        writer.writeheader()
        writer.writerows(output_rows)

        result_key = f'downloads/{bulk_job_id}_output.csv'
        s3.put_object(
            Bucket=settings.AWS_BUCKET_NAME,
            Key=result_key,
            Body=output.getvalue().encode('utf-8')
        )

        job.status = 'completed'
        job.result_s3_key = result_key
        job.completed_at = now()
        job.save(update_fields=['status', 'result_s3_key', 'completed_at'])

        return {"status": "success", "job_id": str(job.id)}

    except SoftTimeLimitExceeded:
        if job:
            job.status = 'failed'
            job.save(update_fields=['status'])
        raise

    except Exception as e:
        if job:
            job.status = 'failed'
            job.save(update_fields=['status'])
        logger.exception(f"[bulk_task={self.request.id}] Unexpected error: {e}")
        raise