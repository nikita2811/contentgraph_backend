from django.urls import path
from .views import (GenerateView,ResultView)

urlpatterns = [
    path("generate-content",        GenerateView.as_view(),    name="content"),
    path("generate/<task_id>", ResultView.as_view(),name="generate"),
]