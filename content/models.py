from django.db import models
from django.contrib.auth import get_user_model
import uuid
User = get_user_model()


class Product(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='product')
    celery_task_id  = models.CharField(max_length=255, null=True, blank=True)
    bulk_job_id     = models.ForeignKey('BulkJob', on_delete=models.SET_NULL, null=True, blank=True)
    product_name    = models.CharField(max_length=255)
    category        = models.CharField(max_length=100, blank=True)  
    tone            = models.CharField(max_length=100, blank=True)
    target_audience = models.CharField(max_length=100,blank=True)    
    key_features = models.JSONField(default=list)
    status          = models.CharField(max_length=100)
    request_type    = models.CharField(max_length=100)
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'product'
        ordering = ['-created_at']

    def __str__(self):
        return self.product_name
    
class AIResult(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.OneToOneField('Product', on_delete=models.CASCADE, related_name='result')
    seo_title = models.CharField(max_length=255)
    meta_description = models.TextField()
    long_description = models.TextField()
    tags = models.JSONField(default=list)
    primary_keyword = models.CharField(max_length=255)
    secondary_keyword = models.CharField(max_length=255)
    generation_time_ms = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'ai_result'

    def __str__(self):
        return f"Result for {self.request.product_name}"

class BulkJob(models.Model):
   
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='bulk_jobs')
    name = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    total_items = models.IntegerField(default=0)
    processed_items = models.IntegerField(default=0)
    failed_items = models.IntegerField(default=0)
    rabbitmq_queue = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'bulk_job'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} - {self.status}"
    


class CeleryTaskMeta(models.Model):
    QUEUE_CHOICES = [('redis', 'Redis'), ('rabbitmq', 'RabbitMQ')]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('started', 'Started'),
        ('success', 'Success'),
        ('failure', 'Failure'),
        ('retrying', 'Retrying'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.OneToOneField('Product', on_delete=models.CASCADE, related_name='task_meta')
    task_id = models.CharField(max_length=255, unique=True)
    task_name = models.CharField(max_length=255)
    queue_type = models.CharField(max_length=20, choices=QUEUE_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    result = models.JSONField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    retry_count = models.IntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'celery_task_meta'

    def __str__(self):
        return f"{self.task_name} - {self.status}"


class BulkJobItem(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    bulk_job = models.ForeignKey('BulkJob', on_delete=models.CASCADE, related_name='items')
    request = models.OneToOneField('Product', on_delete=models.CASCADE, related_name='bulk_item')
    position = models.IntegerField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    error_message = models.TextField(null=True, blank=True)

    class Meta:
        db_table = 'bulk_job_item'
        ordering = ['position']

    def __str__(self):
        return f"Item {self.position} - {self.bulk_job.name} - {self.status}"
    
