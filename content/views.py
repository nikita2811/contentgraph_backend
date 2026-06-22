import logging
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework.views import APIView

from celery.result import AsyncResult
from .tasks import generate_content_task
import json
from .serializers import ProductCreateSerializer,ProductSerializer
from rest_framework.permissions import IsAuthenticated
from .models import CeleryTaskMeta,AIResult,BulkJob,Product
from django.http import (
    FileResponse, JsonResponse, HttpResponseBadRequest, HttpResponseNotFound
)
from django.http import StreamingHttpResponse
import csv
from django.conf import settings
import boto3
from dotenv import load_dotenv
import os
from .csv_task import process_csv_task
from django.views import View
from django.http import HttpResponse
from rest_framework.pagination import PageNumberPagination
 
from django.db.models import Value, FloatField, IntegerField, CharField
from django.db.models import F, Case, When, ExpressionWrapper
from django.db.models.functions import Cast
 
from .models import Product, BulkJob


from django.db.models import Sum, Avg
from django.utils import timezone
from datetime import timedelta
from payment.models import WalletTransaction,APIUsageCharge
from django.db import transaction
from celery.utils import uuid as celery_uuid
load_dotenv()  # loads .env file



logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class GenerateView(APIView):
    permission_classes = [IsAuthenticated]
    """
    POST /api/generate/
    Accepts product details, fires Celery task, returns task_id immediately.
    Cloudflare sees a < 1s response. ✓
    """
   

    def post(self, request):
        try:
            serializer = ProductCreateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            is_regenerate = serializer.validated_data.pop('is_regenerate',True)
            request_data = serializer.save(
                user=request.user,
                request_type='single',
                status="pending",
            )
           
            print(f"{is_regenerate}")
            print(f"{serializer.validated_data}")
            # 1. Reserve a task ID before dispatching
            
            task_id = celery_uuid()
            # 2. Create the meta row FIRST, while still in the transaction
            CeleryTaskMeta.objects.create(
                request=request_data,
                task_id=task_id,
                task_name='generate_seo_content',
                queue_type='default',
                status='pending',
            )
    
            request_data.celery_task_id = task_id
            request_data.save(update_fields=['celery_task_id'])
    
            # 3. Dispatch AFTER the transaction commits — row is guaranteed to exist
            transaction.on_commit(
                lambda: generate_content_task.apply_async(
                    args=[request_data.id, request.user.id],
                    kwargs={'is_regenerate': is_regenerate},
                    task_id=task_id,
                )
            )
    
            logger.info(f"Task queued: {task_id}")
    
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)
    
        return JsonResponse({
            "task_id": task_id,
            "status": "queued",
        }, status=202)
class ResultView(APIView):
    def get(self, request, task_id: str):
        # First check Celery task state
        result = AsyncResult(task_id)

        if result.state == "PENDING":
            return JsonResponse({"status": "pending"})

        if result.state in ("STARTED", "RETRY"):
            return JsonResponse({"status": "processing"})

        if result.state == "FAILURE":
            logger.error(f"Task {task_id} failed: {result.result}")
            return JsonResponse({
                "status": "failed",
                "error": str(result.result),
            }, status=500)

        if result.state == "SUCCESS":
            try:
                # Fetch from DB instead of parsing result
                meta = CeleryTaskMeta.objects.get(task_id=task_id)
                ai_result = AIResult.objects.get(request=meta.request_id)

                return JsonResponse({
                    "status": "done",
                    "seo_title": ai_result.seo_title,
                    "meta_title":ai_result.meta_title,
                    "meta_description": ai_result.meta_description,
                    "tags": ai_result.tags,
                    "primary_keyword": ai_result.primary_keyword,
                    "secondary_keyword": ai_result.secondary_keyword,
                })
            except AIResult.DoesNotExist:
                return JsonResponse({"status": "failed", "error": "Result not found"}, status=404)

        return JsonResponse({"status": result.state.lower()})


class BulkFileProcessor(APIView):
    permission_classes = [IsAuthenticated]
    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
    BUCKET_NAME = 'content.graph'
    def post(self,request):
        file = request.FILES.get('file')

        if not file:
            return HttpResponseBadRequest('No file provided. Send multipart field "file".')
        
        if not file.name.endswith('.csv'):
            return HttpResponseBadRequest('Only .csv files are accepted.')

        if file.size > self.MAX_FILE_SIZE:
            return HttpResponseBadRequest('File too large (max 10 MB).')
        
        job = BulkJob.objects.create(
            status="pending",
            user=request.user,
            name=file.name)
        
        # Upload raw file to S3
        s3_key = f"uploads/{job.id}_{file.name}"
        job.s3_key = s3_key
        job.save(update_fields=['s3_key'])
        try:
            s3 = boto3.client('s3',
                 aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID'),
                 aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY'),
                 region_name=os.environ.get('AWS_DEFAULT_REGION')
                )
            
            s3.upload_fileobj(
                Fileobj=file,
                Bucket=self.BUCKET_NAME,
                Key=s3_key
            )
            process_csv_task.delay(job.id,request.user.id)

           
            
        except Exception as e:
            job.status ='failed'
            job.save(update_fields=['status'])
            return HttpResponseBadRequest(f'S3 upload failed: {e}')
        
        return JsonResponse({"message":"file uploaded successfully",
                             "job_id":job.id})
    

    def get(self,request, job_id):
        s3 = boto3.client('s3',
                 aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID'),
                 aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY'),
                 region_name=os.environ.get('AWS_DEFAULT_REGION')
                )
        try:
            # Make sure job belongs to requesting user
            job = BulkJob.objects.get(id=job_id, user=request.user)
        except BulkJob.DoesNotExist:
            return JsonResponse({'error': 'Job not found'}, status=404)
    
        if job.status != 'completed':
            return JsonResponse({'error': f'Job not ready, current status: {job.status}'}, status=400)
    
        if not job.result_s3_key:
            return JsonResponse({'error': 'Result file not found'}, status=404)
        
         # Generate presigned URL — valid for 1 hour
        try:
         s3_object = s3.get_object(   # ← call get_object, not generate_presigned_url
            Bucket=settings.AWS_BUCKET_NAME,
            Key=job.result_s3_key,
        )
        except Exception as e:
         return JsonResponse({'error': f'S3 fetch failed: {e}'}, status=500)

        response = StreamingHttpResponse(
        s3_object['Body'].iter_chunks(chunk_size=8192),  # ← subscript the response, not the client
        content_type='text/csv',
        )
        response['Content-Disposition'] = f'attachment; filename="result_{job_id}.csv"'
        return response
    


class JobsPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 100

class FetchJobsView(APIView):
    permission_classes = [IsAuthenticated]

    @staticmethod
    def _format_single(product):
        task_meta = getattr(product, "task_meta", None)
        resolved_status = task_meta.status if task_meta else product.status
        progress = 100.0 if resolved_status in ("success", "completed") else 0.0
        return {
            "id":             str(product.id),
            "type":           "single",
            "name":           product.product_name,
            "status":         resolved_status,
            "total_products": 1,
            "progress":       progress,
            "created_at":     product.created_at.isoformat(),
        }
    
    @staticmethod
    def _format_bulk(job):
        total    = job.total_items or 0
        done     = (job.processed_items or 0) + (job.failed_items or 0)
        progress = round((done / total) * 100, 2) if total > 0 else 0.0
        return {
            "id":             str(job.id),
            "type":           "bulk",
            "name":           job.name,
            "status":         job.status,
            "total_products": total,
            "progress":       progress,
            "created_at":     job.created_at.isoformat(),
        }
 
    
    def get(self,request):
        user = request.user

        singles_qs = (
            Product.objects.filter(user=user, request_type="single")
            .annotate(
                _type=Value("single", output_field=CharField()),
                # use product_name as the shared "name" column
                _name=F("product_name"),
                # total_products is always 1 for singles
                _total=Value(1, output_field=IntegerField()),
            )
            .values("id", "_type", "_name", "status", "_total", "created_at")
            .order_by()           # clear default ordering before union
        )

        bulks_qs = (
            BulkJob.objects.filter(user=user)
            .annotate(
                _type=Value("bulk", output_field=CharField()),
                _name=F("name"),
                _total=F("total_items"),
            )
            .values("id", "_type", "_name", "status", "_total", "created_at")
            .order_by()
        )

        combined_qs = singles_qs.union(bulks_qs).order_by("-created_at")

        paginator = JobsPagination()
        page = paginator.paginate_queryset(combined_qs, request, view=self)

        page_ids      = [row["id"] for row in page]
        single_ids    = [row["id"] for row in page if row["_type"] == "single"]
        bulk_ids      = [row["id"] for row in page if row["_type"] == "bulk"]

        singles_map = {
            str(p.id): p
            for p in Product.objects
                .filter(id__in=single_ids)
                .select_related("task_meta")
        }
        bulks_map = {
            str(j.id): j
            for j in BulkJob.objects.filter(id__in=bulk_ids)
        }
 
        results = []
        for row in page:
            row_id = str(row["id"])
            if row["_type"] == "single":
                results.append(self._format_single(singles_map[row_id]))
            else:
                results.append(self._format_bulk(bulks_map[row_id]))
 
        return paginator.get_paginated_response(results)






# ── shared helper ─────────────────────────────────────────────────────────────

def _credits_used(user, since=None):
    """
    Sum of WalletTransaction debit amounts for the user.
    Debits represent credits consumed for API usage.
    Optionally filter to transactions after `since` (datetime).
    """
    qs = APIUsageCharge.objects.filter(
        user_id=user,
        
    )
    qsr = APIUsageCharge.objects.filter(
        user_id=user,
        status="refunded"
    )
    qsf = APIUsageCharge.objects.filter(
        user_id=user,
        status="refunded"
    )
    charged = 0
    if since:
        qs = qs.filter(charged_at__gte=since)
        qsr = qsr.filter(charged_at__gte=since) 
        qsf = qsf.filter(charged_at__gte=since)
    total_charged = qs.aggregate(total=Sum("total_charged"))["total"] or 0
    refunded = qsr.aggregate(total=Sum("total_charged"))["total"] or 0
    failed = qsf.aggregate(total=Sum("total_charged"))["total"] or 0
    refund = refunded + failed
    charged = total_charged - refund
   
    return charged


# ── Dashboard stats ───────────────────────────────────────────────────────────

class DashboardStatsView(APIView):
    """
    GET /api/stats/

    Response:
    {
        "credits_used": {
            "total":      <Decimal>,  -- all-time debits from wallet
            "this_week":  <Decimal>   -- debits in the last 7 days
        },
        "descriptions_generated": {
            "total":      <int>
        },
        "bulk_jobs_run": {
            "total":      <int>,
            "this_month": <int>
        },
        "avg_generation_time": {
            "seconds":    <float>
        }
    }
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        user        = request.user
        now         = timezone.now()
        week_ago    = now - timedelta(days=7)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # ── Credits used ──────────────────────────────────────────────────────
        credits_all_time  = _credits_used(user)
        credits_this_week = _credits_used(user, since=week_ago)

        # ── Descriptions generated ────────────────────────────────────────────
        descriptions_total = AIResult.objects.filter(request__user=user).count()

        # ── Bulk jobs run ─────────────────────────────────────────────────────
        bulk_all_time  = BulkJob.objects.filter(user=user).count()
        bulk_this_month = BulkJob.objects.filter(
            user=user, created_at__gte=month_start
        ).count()

        # ── Avg generation time ───────────────────────────────────────────────
        avg_ms = (
            AIResult.objects.filter(request__user=user)
            .aggregate(avg=Avg("generation_time_ms"))["avg"] or 0
        )

        return JsonResponse({
            "credits_used": {
                "total":     credits_all_time,
                "this_week": credits_this_week,
            },
            "descriptions_generated": {
                "total": descriptions_total,
            },
            "bulk_jobs_run": {
                "total":      bulk_all_time,
                "this_month": bulk_this_month,
            },
            "avg_generation_time": {
                "seconds": round(avg_ms / 1000, 1),
            },
        })


# ── Last 30 days stats ────────────────────────────────────────────────────────

class Last30DaysStatsView(APIView):
    """
    GET /api/stats/last-30-days/

    Response:
    {
        "total_jobs":   <int>,
        "completed":    <int>,
        "failed":       <int>,
        "credits_used": <Decimal>  -- sum of debit transactions in last 30 days
    }
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        user  = request.user
        since = timezone.now() - timedelta(days=30)

        # ── Single jobs ───────────────────────────────────────────────────────
        single_qs        = Product.objects.filter(user=user, request_type="single", created_at__gte=since)
        single_total     = single_qs.count()
        single_completed = single_qs.filter(status="completed").count()
        single_failed    = single_qs.filter(status="failed").count()

        # ── Bulk jobs ─────────────────────────────────────────────────────────
        bulk_qs        = BulkJob.objects.filter(user=user, created_at__gte=since)
        bulk_total     = bulk_qs.count()
        bulk_completed = bulk_qs.filter(status="completed").count()
        bulk_failed    = bulk_qs.filter(status="failed").count()

        # ── Credits used ──────────────────────────────────────────────────────
        credits_used = _credits_used(user, since=since)

        return JsonResponse({
            "total_jobs":   single_total + bulk_total,
            "completed":    single_completed + bulk_completed,
            "failed":       single_failed + bulk_failed,
            "credits_used": credits_used,
        })