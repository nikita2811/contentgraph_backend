from rest_framework import serializers
from django.contrib.auth import get_user_model

from .models import AIResult, Product, BulkJob

User = get_user_model()


class AIResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIResult
        fields = [
            'seo_title', 'meta_description', 'long_description',
            'tags', 'primary_keyword', 'secondary_keyword', 'generation_time_ms',
        ]


class ProductSerializer(serializers.ModelSerializer):
    result = AIResultSerializer(read_only=True)

    class Meta:
        model = Product
        fields = [
            'id', 'product_name', 'category', 'platform', 'target_audience',
            'key_features', 'tone', 'status', 'request_type',
            'celery_task_id', 'bulk_job', 'result',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'status', 'request_type', 'celery_task_id',
            'bulk_job', 'result', 'created_at', 'updated_at',
        ]


class ProductCreateSerializer(serializers.ModelSerializer):
    # blank=True on the model makes these optional, so re-declare as required
    product_name = serializers.CharField(max_length=255)
    platform = serializers.CharField(max_length=50)
    target_audience = serializers.CharField(max_length=255)
    tone = serializers.ListField(child=serializers.CharField(), allow_empty=False)
    key_features = serializers.ListField(
        child=serializers.CharField(),
        allow_empty=False,
    )
    # Not a model field: the view pops it before save()
    is_regenerate = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = Product
        fields = [
            'product_name', 'category', 'platform', 'target_audience',
            'key_features', 'tone', 'is_regenerate',
        ]

    def create(self, validated_data):
        # Safety net: never pass a non-model field to Product.objects.create()
        validated_data.pop('is_regenerate', None)
        return super().create(validated_data)