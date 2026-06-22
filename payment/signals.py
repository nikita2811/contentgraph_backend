# billing/signals.py

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.conf import settings
from django.db import transaction

from .models import UserWallet, WalletTransaction

SIGNUP_BONUS = 10  # credits


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_user_wallet(sender, instance, created, **kwargs):
    if not created:
        return

    with transaction.atomic():
        wallet = UserWallet.objects.create(
            user=instance,
            balance=SIGNUP_BONUS,
            total_credited=SIGNUP_BONUS,
            currency='INR',
        )

        WalletTransaction.objects.create(
            wallet=wallet,
            transaction_type='credit',
            amount=SIGNUP_BONUS,
            balance_before=0,
            balance_after=SIGNUP_BONUS,
            description='Welcome bonus — 10 free credits on signup',
        )