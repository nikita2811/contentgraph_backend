# from django.shortcuts import render
# from services.fastapi_client import generate_content
# from rest_framework.response import Response
# from rest_framework.views import APIView

# class GenerateSingleView(APIView):
#     def post(self, request):
#        product_name =request.data.get('product_name')
#        category=request.data.get('category')
#        tone=request.data.get('tone')
#        audience=request.data.get('target_audience')
#        key_features=request.data.get('key_features')
#        payload={
#            "product_name":product_name,
#            "category":category,
#            "tone":tone,
#            "target_audience":audience,
#            "key_features":key_features
           
#        }
#        api_call = generate_content(payload)
#        return Response({
#            "response":api_call
#        })
    
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
from .models import CeleryTaskMeta

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
                        queue_type='redis',       # single API uses redis
                        status='pending',
                    )
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        return JsonResponse({
            "task_id": task.id,
            "status": "queued",
        }, status=202)   # 202 Accepted — work is in progress


class ResultView(APIView):
    """
    GET /content/generate/<task_id>/
    Frontend polls this every 3s. Each call is < 1s. Cloudflare safe. ✓
    """
    def get(self, request, task_id: str):
        result = AsyncResult(task_id)

        # PENDING = not started or unknown task_id
        if result.state == "PENDING":
            return JsonResponse({"status": "pending"})

        # STARTED / RETRY
        if result.state in ("STARTED", "RETRY"):
            return JsonResponse({"status": "processing"})

        # SUCCESS
        if result.state == "SUCCESS":
            # Assuming `response` is the full dict above
            response = result.result
            final_content_str = response["final_content"][0]["text"]
            serp_str = response["serp"][0]["text"]
            
            # Parse the nested JSON strings
            content = json.loads(final_content_str)
            serp = json.loads(serp_str)

            return JsonResponse({
                "status": "done",
                # Extract fields
                "seo_title"       : content["seo_title"],
                "meta_description" : content["meta_description"],
                "meta_title"      : content["h1"]  ,         # no "meta_title" key, h1 is the closest
                "tags"             : content["tags"],
                
                # Keywords from serp
                "primary_keyword"    : serp["primary_keyword"],
                "secondary_keywords"  : serp["secondary_keywords"],
                # the dict returned by your task
            })
        print(f"result data:{result}")

        # FAILURE
        if result.state == "FAILURE":
            logger.error(f"Task {task_id} failed: {result.result}")
            return JsonResponse({
                "status": "failed",
                "error": str(result.result),
            }, status=500)

        return JsonResponse({"status": result.state.lower()})
