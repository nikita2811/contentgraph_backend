from rest_framework import serializers
from django.contrib.auth import get_user_model
from datetime import timezone,timedelta
from .models import AIResult,Product,BulkJob


User = get_user_model()

class AIResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIResult
        fields = [
            'seo_title', 'meta_description', 'long_description',
            'tags', 'primary_keyword', 'secondary_keyword', 'generation_time_ms'
        ]

class ProductSerializer(serializers.ModelSerializer):
    result = AIResultSerializer(read_only=True)

    class Meta:
        model = Product
        fields = [
            'id', 'product_name', 'category', 'target_audience',
            'key_features', 'tone', 'status', 'request_type',
            'celery_task_id', 'bulk_job', 'result',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'status', 'request_type', 'celery_task_id',
            'bulk_job', 'result', 'created_at', 'updated_at'
        ]
class ProductCreateSerializer(serializers.ModelSerializer):
    key_features = serializers.ListField(
    child=serializers.CharField(), allow_empty=False
)
    class Meta:
        model = Product
        fields = [
            'product_name', 'category', 'target_audience',
            'key_features', 'tone', 'celery_task_id'
        ]
    def validate_key_features(self, value):
        if not isinstance(value, list) or len(value) == 0:
            raise serializers.ValidationError("key_features must be a non-empty list.")
        return value

    def validate_tone(self, value):
        allowed = ['professional', 'casual', 'formal', 'friendly']
        if value.lower() not in allowed:
            raise serializers.ValidationError(f"tone must be one of: {', '.join(allowed)}")
        return value.lower()