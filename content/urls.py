from django.urls import path
from .views import (GenerateView,ResultView,BulkFileProcessor)

urlpatterns = [
    path("generate-content",        GenerateView.as_view(),    name="content"),
    path("generate/<task_id>", ResultView.as_view(),name="generate"),
    path("upload-file",BulkFileProcessor.as_view(),name="upload"),
    path("download/<job_id>", BulkFileProcessor.as_view(), name="download_result"),
]