from decimal import Decimal
from django.db import transaction

from payment.models import PricingPlan, APIUsageCharge,WalletTransaction
from .wallet_service import WalletService, InsufficientBalanceError
from django.db.models import Q, Sum


class BillingService:

    @staticmethod
    def get_applicable_plan(units: int) -> "PricingPlan":
        """
        Finds the correct pricing tier for the given unit count.
        Picks the plan where min_units <= units AND (max_units >= units OR max_units is null).
        Orders by min_units descending to get the most specific (highest) matching tier.
        """
        plan = (
            PricingPlan.objects.filter(
                is_active=True,
                min_units__lte=units,
            )
            .filter(
                Q(max_units__gte=units) | Q(max_units__isnull=True)
            )
            .order_by("-min_units")
            .first()
        )
    
        if plan is None:
            raise ValueError(f"No active pricing plan found for {units} units")
    
        return plan

    @staticmethod
    @transaction.atomic
    def charge_for_usage(
        user,
        units: int = 1,
        product_request=None,
        bulk_job=None,
    ) -> APIUsageCharge:
        """
        Main entry point called by your content generation pipeline.
        1. Resolves pricing tier
        2. Debits wallet
        3. Creates APIUsageCharge linked to the WalletTransaction
        """
        plan = BillingService.get_applicable_plan(units)
        total = (plan.price_per_unit * units).quantize(Decimal("0.01"))

        description = (
            f" API usage: {units} unit(s) "
            f"@ ₹{plan.price_per_unit}/unit"
        )

        # Raises InsufficientBalanceError if wallet is low — let caller handle it
        wallet_txn = WalletService.debit_wallet(
            user=user,
            amount=total,
            description=description,
            product_request=product_request,
            bulk_job=bulk_job,
        )

        charge = APIUsageCharge.objects.create(
            user=user,
            product_request=product_request,
            bulk_job=bulk_job,
            pricing_plan=plan,
            wallet_transaction=wallet_txn,
            units_consumed=units,
            unit_price=plan.price_per_unit,
            total_charged=total,
            status="success",
        )
        return charge

    @staticmethod
    @transaction.atomic
    def refund_failed_job(charge: APIUsageCharge, product_request=None, bulk_job=None):
        """
        Called when a content job fails after it was already charged.
        Refunds wallet and marks the charge as refunded.
        """
        WalletService.refund_to_wallet(
            user=charge.user,
            amount=charge.total_charged,
            description=f"Refund for failed job",
            product_request=product_request,
            bulk_job=bulk_job,
        )
        charge.status = "refunded"
        charge.save(update_fields=["status"])

    @staticmethod
    @transaction.atomic
    def refund_partial(
        charge: APIUsageCharge,
        failed_units: int,
        bulk_job=None,
    ) -> WalletTransaction | None:
        if failed_units <= 0:
            return None
    
        if failed_units > charge.units_consumed:
            raise ValueError(
                f"failed_units ({failed_units}) cannot exceed "
                f"units_consumed ({charge.units_consumed})"
            )
    
        refund_amount = (charge.unit_price * failed_units).quantize(Decimal("0.01"))
    
        wallet_txn = WalletService.refund_to_wallet(
            user=charge.user,
            amount=refund_amount,
            description=(
                f"Partial refund: {failed_units} of {charge.units_consumed} "
                f"units failed @ ₹{charge.unit_price}/unit"
            ),
            bulk_job=bulk_job,
        )
    
        charge.units_consumed -= failed_units
        charge.total_charged -= refund_amount
    
        # Mark fully refunded charges so already_charged() and get_charge()
        # don't return them on retries, preventing double-refund
        if charge.units_consumed == 0:
            charge.status = 'refunded'
            charge.save(update_fields=["units_consumed", "total_charged", "status"])
        else:
            charge.status = 'failed'
            charge.save(update_fields=["units_consumed", "total_charged","status"])
    
        return wallet_txn
    
    @staticmethod
    def already_charged(bulk_job=None, product_request=None) -> bool:
        """
        Check if a successful charge already exists.
        Prevents double charging on Celery task retries.
        """
        if bulk_job is not None:
            return APIUsageCharge.objects.filter(
                bulk_job=bulk_job,
                status='success',
            ).exists()
    
        if product_request is not None:
            return APIUsageCharge.objects.filter(
                product_request=product_request,
                status='success',
            ).exists()
    
        return False
    
    
    @staticmethod
    def get_charge(bulk_job=None, product_request=None):
        """
        Retrieve existing charge for use in refund_partial on retry.
        """
        if bulk_job is not None:
            return APIUsageCharge.objects.filter(
                bulk_job=bulk_job,
                status='success',
            ).order_by('-charged_at').first()
    
        if product_request is not None:
            return APIUsageCharge.objects.filter(
                product_request=product_request,
                status='success',
            ).order_by('-charged_at').first()
    
        return None