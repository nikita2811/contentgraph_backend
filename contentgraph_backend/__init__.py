# core/__init__.py  — makes Celery load with Django
from .celery import app as celery_app
__default__ = ("celery_app",)