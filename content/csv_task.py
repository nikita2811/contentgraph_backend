import csv
import io
import boto3
from django.conf import settings
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from contentgraph_backend.exceptions import AIServiceUnavailable, AIServiceError
from services.fastapi_client import generate_content
from .models import Product, CeleryTaskMeta, AIResult
import boto3
from django.utils.timezone import now
from .models import BulkJob
import logging
import json
import uuid
from .models import BulkJobItem

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
        obj = s3.get_object(Bucket='content.graph', Key=job.s3_key)
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
                    'queue_type': 'rabbitmq',
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
                response = generate_content(product_details)

                final_content_str = response["final_content"][0]["text"]
                serp_str = response["serp"][0]["text"]
                content = json.loads(final_content_str)
                serp = json.loads(serp_str)
                print("SERP KEYS:", serp.keys())  # ← add this
                print("SERP DATA:", serp)         # ← and this
                # Save AIResult per row
                AIResult.objects.get_or_create(
                  request=product,
                  defaults={
                      'seo_title': content.get("seo_title") or "",
                      'meta_description': content.get("meta_description") or "",
                      'meta_title': content.get("h1") or content.get("meta_title") or "",
                      'long_description': content.get("intro_paragraph") or content.get("introduction") or "",
                      'tags': content.get("tags") or [],
                      'primary_keyword': serp.get("primary_keyword") or "",        # ✅
                      'secondary_keyword': ", ".join(serp.get("secondary_keywords") or []),  # ✅
                  }
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

        result_key = f'downloads/{bulk_job_id}/output.csv'
        s3.put_object(
            Bucket='content.graph',
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