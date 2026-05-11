import uuid
from django.db import models


class PricingPlan(models.Model):
    API_TYPE_CHOICES = [
        ('single', 'Single'),
        ('bulk', 'Bulk'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    api_type = models.CharField(max_length=10, choices=API_TYPE_CHOICES)
    tier_name = models.CharField(max_length=100)
    min_units = models.IntegerField(default=1)
    max_units = models.IntegerField(null=True, blank=True)  # null = unlimited
    price_per_unit = models.DecimalField(max_digits=10, decimal_places=4)
    currency = models.CharField(max_length=3, default='INR')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'pricing_plan'
        ordering = ['api_type', 'min_units']

    def __str__(self):
        return f"{self.tier_name} ({self.api_type}) - {self.price_per_unit}/{self.currency}"


class UserWallet(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField('auth.User', on_delete=models.CASCADE, related_name='wallet')
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_credited = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_debited = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default='INR')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'user_wallet'

    def __str__(self):
        return f"{self.user.email} - {self.balance} {self.currency}"


class RazorpayOrder(models.Model):
    STATUS_CHOICES = [
        ('created', 'Created'),
        ('attempted', 'Attempted'),
        ('paid', 'Paid'),
        ('failed', 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='razorpay_orders')
    razorpay_order_id = models.CharField(max_length=255, unique=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)  # in INR (not paise)
    currency = models.CharField(max_length=3, default='INR')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='created')
    receipt = models.CharField(max_length=255, unique=True)  # your internal reference
    notes = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'razorpay_order'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.razorpay_order_id} - {self.status}"


class RazorpayPayment(models.Model):
    METHOD_CHOICES = [
        ('card', 'Card'),
        ('netbanking', 'Net Banking'),
        ('upi', 'UPI'),
        ('wallet', 'Wallet'),
        ('emi', 'EMI'),
    ]
    STATUS_CHOICES = [
        ('created', 'Created'),
        ('authorized', 'Authorized'),
        ('captured', 'Captured'),
        ('refunded', 'Refunded'),
        ('failed', 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey('RazorpayOrder', on_delete=models.CASCADE, related_name='payments')
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='razorpay_payments')
    razorpay_payment_id = models.CharField(max_length=255, unique=True)
    razorpay_signature = models.CharField(max_length=500)  # for HMAC verification
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default='INR')
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='created')
    webhook_payload = models.JSONField(default=dict, blank=True)  # raw Razorpay event
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'razorpay_payment'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.razorpay_payment_id} - {self.status}"


class WalletTransaction(models.Model):
    TRANSACTION_TYPE_CHOICES = [
        ('credit', 'Credit'),   # top-up
        ('debit', 'Debit'),     # API usage charge
        ('refund', 'Refund'),   # failed job refund
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey('UserWallet', on_delete=models.CASCADE, related_name='transactions')
    payment = models.OneToOneField('RazorpayPayment', on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='wallet_transaction')
    seo_request = models.ForeignKey('SEORequest', on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='wallet_transactions')
    bulk_job = models.ForeignKey('BulkJob', on_delete=models.SET_NULL,
                                 null=True, blank=True, related_name='wallet_transactions')
    transaction_type = models.CharField(max_length=10, choices=TRANSACTION_TYPE_CHOICES)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    balance_before = models.DecimalField(max_digits=12, decimal_places=2)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2)
    description = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'wallet_transaction'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.transaction_type} - {self.amount} ({self.wallet.user.email})"


class APIUsageCharge(models.Model):
    API_TYPE_CHOICES = [
        ('single', 'Single'),
        ('bulk', 'Bulk'),
    ]
    STATUS_CHOICES = [
        ('success', 'Success'),
        ('failed', 'Failed'),
        ('refunded', 'Refunded'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='usage_charges')
    seo_request = models.ForeignKey('SEORequest', on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='usage_charges')
    bulk_job = models.ForeignKey('BulkJob', on_delete=models.SET_NULL,
                                 null=True, blank=True, related_name='usage_charges')
    pricing_plan = models.ForeignKey('PricingPlan', on_delete=models.PROTECT,
                                     related_name='usage_charges')
    wallet_transaction = models.OneToOneField('WalletTransaction', on_delete=models.SET_NULL,
                                              null=True, blank=True, related_name='usage_charge')
    api_type = models.CharField(max_length=10, choices=API_TYPE_CHOICES)
    units_consumed = models.IntegerField(default=1)  # 1 for single, N for bulk
    unit_price = models.DecimalField(max_digits=10, decimal_places=4)  # snapshot at charge time
    total_charged = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='success')
    charged_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'api_usage_charge'
        ordering = ['-charged_at']

    def __str__(self):
        return f"{self.api_type} - {self.units_consumed} units - {self.total_charged}"