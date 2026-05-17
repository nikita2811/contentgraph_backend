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
from .models import CeleryTaskMeta,AIResult,BulkJob
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
            request_data = serializer.save(user=request.user, 
                                      request_type='single',
                                      status="pending",
                                      )
            task = generate_content_task.delay(request_data.id)
            logger.info(f"Task dispatched: {task.id}")
            request_data.celery_task_id=task.id
            request_data.save(update_fields=['celery_task_id'])

            CeleryTaskMeta.objects.create(
                        request=request_data,
                        task_id=task.id,
                        task_name='generate_seo_content',
                        queue_type='default',       # single API uses redis
                        status='pending',
                    )
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        return JsonResponse({
            "task_id": task.id,
            "status": "queued",
        }, status=202)   # 202 Accepted — work is in progress


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
            raise HttpResponseBadRequest('No file provided. Send multipart field "file".')
        
        if not file.name.endswith('.csv'):
            return HttpResponseBadRequest('Only .csv files are accepted.')

        if file.size > self.MAX_FILE_SIZE:
            return HttpResponseBadRequest('File too large (max 10 MB).')
        
        job = BulkJob.objects.create(
            status="pending",
            user=request.user)
        
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
            process_csv_task.delay(job.id)

           
            
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
       
