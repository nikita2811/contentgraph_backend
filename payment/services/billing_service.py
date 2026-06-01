from decimal import Decimal
from django.db import transaction

from payment.models import PricingPlan, APIUsageCharge
from .wallet_service import WalletService, InsufficientBalanceError


class BillingService:

    @staticmethod
    def get_applicable_plan(api_type: str, units: int) -> PricingPlan:
        """
        Finds the correct pricing tier for the given api_type and unit count.
        Tiers are ordered by min_units ascending; pick the last one where
        min_units <= units (and max_units >= units or max_units is null).
        """
        plans = PricingPlan.objects.filter(
            api_type=api_type,
            is_active=True,
            min_units__lte=units,
        ).order_by("-min_units")

        for plan in plans:
            if plan.max_units is None or plan.max_units >= units:
                return plan

        raise ValueError(f"No active pricing plan for {api_type} with {units} units")

    @staticmethod
    @transaction.atomic
    def charge_for_usage(
        user,
        api_type: str,
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
        plan = BillingService.get_applicable_plan(api_type, units)
        total = (plan.price_per_unit * units).quantize(Decimal("0.01"))

        description = (
            f"{api_type.capitalize()} API usage: {units} unit(s) "
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
            api_type=api_type,
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
            description=f"Refund for failed {charge.api_type} job",
            product_request=product_request,
            bulk_job=bulk_job,
        )
        charge.status = "refunded"
        charge.save(update_fields=["status"])