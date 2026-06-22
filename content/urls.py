from django.urls import path
from .views import (GenerateView,ResultView,BulkFileProcessor,FetchJobsView,DashboardStatsView,Last30DaysStatsView)

urlpatterns = [
    path("generate-content",        GenerateView.as_view(),    name="content"),
    path("generate/<task_id>", ResultView.as_view(),name="generate"),
    path("upload-file",BulkFileProcessor.as_view(),name="upload"),
    path("download/<uuid:job_id>", BulkFileProcessor.as_view(), name="download_result"),
    # path("dashboard", DashboardView.as_view(), name="dashboard"),
    path("jobs", FetchJobsView.as_view(), name="job-list"),
    path("dashboard-stats",DashboardStatsView.as_view(),name="stats"),
    path("job-history-stats",Last30DaysStatsView.as_view(),name="job-stats")
]