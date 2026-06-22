import csv
import io
import boto3
from django.conf import settings
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from contentgraph_backend.exceptions import AIServiceUnavailableError, AIServiceFailedError
from services.fastapi_client import generate_content
from .models import Product, CeleryTaskMeta, AIResult, BulkJob, BulkJobItem, TokenUsage
from django.utils.timezone import now
import logging
import json
import uuid
import time
from payment.services.billing_service import BillingService
from payment.services.wallet_service import InsufficientBalanceError
from django.contrib.auth import get_user_model
from django.db.models import F

logger = logging.getLogger(__name__)
User = get_user_model()

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


def _refund_unattempted(job: BulkJob, charge):
    """
    Single reconciliation point for all refunds.
    Refunds total_items - processed_items units — everything that was charged
    upfront but did not complete successfully, regardless of failure reason.
    Guards against double-refund by checking charge.status before proceeding.
    """
    if charge is None:
        logger.warning(f"[_refund_unattempted] charge is None for job {job.id}, skipping")
        return

    try:
        # Refresh charge from DB — if already fully refunded on a prior call, skip
        charge.refresh_from_db(fields=['status', 'units_consumed'])
        if charge.status == 'refunded':
            logger.info(
                f"[_refund_unattempted] charge already fully refunded for job {job.id}, skipping"
            )
            return

        job.refresh_from_db(fields=['processed_items', 'total_items'])
        unprocessed = job.total_items - job.processed_items

        logger.info(
            f"[_refund_unattempted] job={job.id} total={job.total_items} "
            f"processed={job.processed_items} unprocessed={unprocessed}"
        )

        if unprocessed > 0:
            BillingService.refund_partial(
                charge=charge,
                failed_units=unprocessed,
                bulk_job=job,
            )
            logger.info(
                f"[_refund_unattempted] refunded {unprocessed} units for job {job.id}"
            )
    except Exception as refund_error:
        logger.exception(f"[_refund_unattempted] failed for job {job.id}: {refund_error}")


def _mark_failed(seo_request, meta, error_message, retries=0):
    if seo_request is not None:
        seo_request.status = 'failed'
        seo_request.save(update_fields=['status'])
    meta.status = 'failure'
    meta.error_message = error_message
    meta.retry_count = retries
    meta.completed_at = now()
    meta.save(update_fields=['status', 'error_message', 'retry_count', 'completed_at'])


def _handle_row_failure(position, row_error, job, bulk_item, meta):
    """
    Mark row as failed and increment counter.
    No per-row refund here — all refunds are handled in one shot by
    _refund_unattempted at job end, using total_items - processed_items.
    """
    logger.error(f"[process_csv_task] row {position} failed: {row_error}")

    if bulk_item is not None:
        bulk_item.status = 'failed'
        bulk_item.error_message = str(row_error)
        bulk_item.save(update_fields=['status', 'error_message'])

    if meta is not None:
        meta.status = 'failure'
        meta.error_message = str(row_error)
        meta.completed_at = now()
        meta.save(update_fields=['status', 'error_message', 'completed_at'])

    BulkJob.objects.filter(id=job.id).update(failed_items=F('failed_items') + 1)


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=5,
    soft_time_limit=570,
    time_limit=600,
    acks_late=True,
    queue='csv'
)
def process_csv_task(self, bulk_job_id, user_id) -> dict:

    job = None
    charge = None

    try:
        job = BulkJob.objects.get(id=bulk_job_id)
        user = User.objects.get(id=user_id)

        if job.status == 'completed':
            return {"status": "already_completed", "job_id": str(job.id)}

        job.status = 'processing'
        job.save(update_fields=['status'])

        obj = s3.get_object(Bucket=settings.AWS_BUCKET_NAME, Key=job.s3_key)
        content_file = obj['Body'].read().decode('utf-8')
        reader = list(csv.DictReader(io.StringIO(content_file)))

        job.total_items = len(reader)
        job.save(update_fields=['total_items'])

        output_rows = []

        # ── Consolidated charge block ──────────────────────────────────────────
        # Both charge_for_usage and get_charge are inside a single try so that
        # any failure here leaves charge=None (nothing deducted) and the outer
        # handler skips the refund correctly.
        # ──────────────────────────────────────────────────────────────────────
        try:
            if not BillingService.already_charged(bulk_job=job):
                charge = BillingService.charge_for_usage(
                    user=user,
                    units=job.total_items,
                    bulk_job=job,
                )
                logger.info(
                    f"[process_csv_task] charged {job.total_items} units "
                    f"for job {job.id}, charge={charge.id}"
                )
            else:
                charge = BillingService.get_charge(bulk_job=job)
                logger.info(
                    f"[process_csv_task] existing charge found for job {job.id}, "
                    f"charge={charge.id if charge else None}"
                )
        except InsufficientBalanceError:
            job.status = 'failed'
            job.save(update_fields=['status'])
            return {"status": "insufficient_balance", "job_id": str(job.id)}
        except Exception as charge_error:
            # Unexpected failure — nothing was deducted, charge stays None,
            # outer handler will skip the refund safely.
            logger.exception(
                f"[process_csv_task] failed to resolve charge for job {job.id}: {charge_error}"
            )
            job.status = 'failed'
            job.save(update_fields=['status'])
            raise

        # ── Row loop ───────────────────────────────────────────────────────────
        for position, row in enumerate(reader):
            row = normalize_row(row)
            bulk_item = None
            meta = None

            try:
                product, created = Product.objects.get_or_create(
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
                if not created and product.status == 'failed':
                    product.status = 'pending'
                    product.save(update_fields=['status'])

                bulk_item, _ = BulkJobItem.objects.get_or_create(
                    bulk_job=job,
                    request=product,
                    defaults={
                        'position': position,
                        'row_index': position,
                        'status': 'processing',
                    }
                )

                meta, created = CeleryTaskMeta.objects.get_or_create(
                    request=product,
                    defaults={
                        'task_id': str(uuid.uuid4()),
                        'task_name': 'process_csv_row',
                        'queue_type': 'csv',
                        'status': 'started',
                        'started_at': now(),
                        'task_meta': bulk_item,
                    }
                )
                if not created:
                    meta.task_id = str(uuid.uuid4())
                    meta.status = 'started'
                    meta.started_at = now()
                    meta.save(update_fields=['task_id', 'status', 'started_at'])

                product_details = {
                    "product_name": product.product_name,
                    "category": product.category,
                    "target_audience": product.target_audience,
                    "tone": product.tone,
                    "key_features": (
                        product.key_features
                        if isinstance(product.key_features, list)
                        else parse_key_features(product.key_features)
                    ),
                }

                logger.info(f"[process_csv_task] processing row {position}: {product_details}")
                start = time.time()

                # ── AI call ────────────────────────────────────────────────────
                # AIServiceUnavailableError → service is down, abort the entire
                #   job immediately and trigger a single bulk refund for all
                #   unprocessed rows via _refund_unattempted.
                # AIServiceFailedError     → row-level failure only, re-raise
                #   as plain Exception so the loop continues to the next row.
                # ──────────────────────────────────────────────────────────────
                try:
                    response = generate_content(product_details)
                except AIServiceUnavailableError as e:
                    logger.error(
                        f"[process_csv_task] AI service unavailable at row {position}, "
                        f"aborting job {job.id}: {e}"
                    )
                    _handle_row_failure(position, e, job, bulk_item, meta)
                    _refund_unattempted(job, charge)
                    job.status = 'failed'
                    job.save(update_fields=['status'])
                    return {"status": "ai_unavailable", "job_id": str(job.id)}
                except AIServiceFailedError as e:
                    raise Exception(f"AI service error: {str(e)}") from e

                elapsed_ms = int((time.time() - start) * 1000)

                token_usage = response['token_usage']
                AIResult.objects.update_or_create(
                    request=product,
                    defaults={
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

                bulk_item.status = 'completed'
                bulk_item.save(update_fields=['status'])

                meta.status = 'success'
                meta.completed_at = now()
                meta.save(update_fields=['status', 'completed_at'])

                BulkJob.objects.filter(id=job.id).update(processed_items=F('processed_items') + 1)

                output_rows.append({
                    'product_name': product.product_name,
                    'seo_title': response['seo_title'],
                    'meta_description': response['meta_description'],
                    'meta_title': response['meta_title'],
                    'long_description': response['intro_paragraph'],
                    'tags': response['tags'] if isinstance(response['tags'], list) else response['tags'].split(","),
                    'primary_keyword': response['primary_keyword'],
                    'secondary_keyword': ",".join(response['secondary_keyword']) if isinstance(response['secondary_keyword'], list) else response['secondary_keyword'],
                   
                })

            except Exception as row_error:
                logger.exception(f"[process_csv_task] row {position} failed: {row_error}")
                _handle_row_failure(position, row_error, job, bulk_item, meta)
                continue

        # ── Write result CSV to S3 ─────────────────────────────────────────────
        print(f'{output_rows}')
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=['product_name', 'seo_title', 'meta_title', 'meta_description', 'long_description','primary_keyword','secondary_keyword', 'tags']
        )
        writer.writeheader()
        writer.writerows(output_rows)

        result_key = f'downloads/{bulk_job_id}_output.csv'
        s3.put_object(
            Bucket=settings.AWS_BUCKET_NAME,
            Key=result_key,
            Body=output.getvalue().encode('utf-8')
        )

        # Single reconciliation point — refunds everything not in processed_items
        _refund_unattempted(job, charge)

        job.status = 'completed'
        job.result_s3_key = result_key
        job.completed_at = now()
        job.save(update_fields=['status', 'result_s3_key', 'completed_at'])

        return {"status": "success", "job_id": str(job.id)}

    except SoftTimeLimitExceeded:
        if job:
            job.status = 'failed'
            job.save(update_fields=['status'])
            _refund_unattempted(job, charge)
        raise

    except Exception as e:
        if job:
            job.status = 'failed'
            job.save(update_fields=['status'])
            _refund_unattempted(job, charge)
        logger.exception(f"[bulk_task={self.request.id}] Unexpected error: {e}")
        raise