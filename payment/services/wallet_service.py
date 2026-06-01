from decimal import Decimal
from django.db import transaction
from django.utils import timezone

from payment.models import UserWallet, WalletTransaction, RazorpayPayment, APIUsageCharge, PricingPlan


class InsufficientBalanceError(Exception):
    pass


class WalletService:

    @staticmethod
    @transaction.atomic
    def credit_wallet(
        payment: RazorpayPayment,
        description: str = "Wallet top-up via Razorpay",
    ) -> WalletTransaction:
        """
        Called after a successful payment verification.
        Credits the wallet and creates a WalletTransaction.
        Uses select_for_update to prevent race conditions.
        """
        wallet = (
            UserWallet.objects
            .select_for_update()
            .get(user=payment.user)
        )
        amount = payment.amount
        balance_before = wallet.balance

        wallet.balance += amount
        wallet.total_credited += amount
        wallet.save()

        txn = WalletTransaction.objects.create(
            wallet=wallet,
            payment=payment,
            transaction_type="credit",
            amount=amount,
            balance_before=balance_before,
            balance_after=wallet.balance,
            description=description,
        )
        return txn

    @staticmethod
    @transaction.atomic
    def debit_wallet(
        user,
        amount: Decimal,
        description: str,
        product_request=None,
        bulk_job=None,
    ) -> WalletTransaction:
        """
        Deducts from wallet. Raises InsufficientBalanceError if balance is too low.
        """
        wallet = (
            UserWallet.objects
            .select_for_update()
            .get(user=user)
        )
        if wallet.balance < amount:
            raise InsufficientBalanceError(
                f"Insufficient balance: {wallet.balance} < {amount}"
            )

        balance_before = wallet.balance
        wallet.balance -= amount
        wallet.total_debited += amount
        wallet.save()

        txn = WalletTransaction.objects.create(
            wallet=wallet,
            transaction_type="debit",
            amount=amount,
            balance_before=balance_before,
            balance_after=wallet.balance,
            description=description,
            product_request=product_request,
            bulk_job=bulk_job,
        )
        return txn

    @staticmethod
    @transaction.atomic
    def refund_to_wallet(
        user,
        amount: Decimal,
        description: str,
        product_request=None,
        bulk_job=None,
    ) -> WalletTransaction:
        """
        Reverses a failed job charge back into the wallet.
        """
        wallet = (
            UserWallet.objects
            .select_for_update()
            .get(user=user)
        )
        balance_before = wallet.balance
        wallet.balance += amount
        wallet.total_credited += amount
        wallet.save()

        txn = WalletTransaction.objects.create(
            wallet=wallet,
            transaction_type="refund",
            amount=amount,
            balance_before=balance_before,
            balance_after=wallet.balance,
            description=description,
            product_request=product_request,
            bulk_job=bulk_job,
        )
        return txn