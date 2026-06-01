from django.shortcuts import render
import uuid
import json
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework import status

from .models import RazorpayOrder, RazorpayPayment, UserWallet
from .services.razorpay_service import RazorpayService
from .services.wallet_service import WalletService

razorpay_svc = RazorpayService()


class CreateOrderView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        amount = request.data.get("amount")  # INR, e.g. 500.00
        if not amount or float(amount) <= 0:
            return Response(
                {"error": "Invalid amount"}, status=status.HTTP_400_BAD_REQUEST
            )

        receipt = f"rcpt_{uuid.uuid4().hex[:16]}"
        notes = {"user_id": str(request.user.id), "email": request.user.email}

        rz_order = razorpay_svc.create_order(
            amount_inr=float(amount),
            receipt=receipt,
            notes=notes,
        )

        db_order = RazorpayOrder.objects.create(
            user=request.user,
            razorpay_order_id=rz_order["id"],
            amount=amount,
            currency="INR",
            receipt=receipt,
            notes=notes,
            status="created",
        )

        return Response({
            "order_id": rz_order["id"],       # pass to Razorpay JS SDK
            "amount": rz_order["amount"],     # in paise
            "currency": rz_order["currency"],
            "receipt": receipt,
            "key": settings.RAZORPAY_KEY_ID,  # public key for frontend
        })


class VerifyPaymentView(APIView):
    """
    Called by the frontend after the Razorpay modal succeeds.
    Verifies HMAC signature → credits wallet.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        rz_order_id = request.data.get("razorpay_order_id")
        rz_payment_id = request.data.get("razorpay_payment_id")
        rz_signature = request.data.get("razorpay_signature")

        if not all([rz_order_id, rz_payment_id, rz_signature]):
            return Response(
                {"error": "Missing payment fields"}, status=status.HTTP_400_BAD_REQUEST
            )

        # 1. Verify HMAC
        if not razorpay_svc.verify_payment_signature(
            rz_order_id, rz_payment_id, rz_signature
        ):
            return Response(
                {"error": "Invalid signature"}, status=status.HTTP_400_BAD_REQUEST
            )

        # 2. Fetch our DB order
        try:
            db_order = RazorpayOrder.objects.get(
                razorpay_order_id=rz_order_id, user=request.user
            )
        except RazorpayOrder.DoesNotExist:
            return Response({"error": "Order not found"}, status=status.HTTP_404_NOT_FOUND)

        # 3. Idempotency: already processed?
        if db_order.status == "paid":
            return Response({"message": "Already processed"})

        # 4. Fetch payment details from Razorpay to get method etc.
        rz_payment = razorpay_svc.fetch_payment(rz_payment_id)

        # 5. Create RazorpayPayment record
        payment = RazorpayPayment.objects.create(
            order=db_order,
            user=request.user,
            razorpay_payment_id=rz_payment_id,
            razorpay_signature=rz_signature,
            amount=db_order.amount,
            currency="INR",
            method=rz_payment.get("method"),
            status="captured",
            paid_at=timezone.now(),
        )

        # 6. Update order status
        db_order.status = "paid"
        db_order.save(update_fields=["status"])

        # 7. Credit wallet
        WalletService.credit_wallet(payment=payment)

        wallet = UserWallet.objects.get(user=request.user)
        return Response({
            "message": "Payment successful. Wallet credited.",
            "wallet_balance": str(wallet.balance),
        })


@method_decorator(csrf_exempt, name="dispatch")
class RazorpayWebhookView(APIView):
    """
    Async webhook handler — idempotent, signature-verified.
    Handles payment.captured as a fallback if VerifyPaymentView wasn't called.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        payload_body = request.body
        signature = request.headers.get("X-Razorpay-Signature", "")

        if not razorpay_svc.verify_webhook_signature(payload_body, signature):
            return Response({"error": "Invalid signature"}, status=status.HTTP_400_BAD_REQUEST)

        event = json.loads(payload_body)
        event_type = event.get("event")

        if event_type == "payment.captured":
            self._handle_payment_captured(event)
        elif event_type == "payment.failed":
            self._handle_payment_failed(event)

        return Response({"status": "ok"})

    def _handle_payment_captured(self, event: dict):
        payload = event["payload"]["payment"]["entity"]
        rz_payment_id = payload["id"]
        rz_order_id = payload["order_id"]

        # Idempotency: skip if already recorded
        if RazorpayPayment.objects.filter(razorpay_payment_id=rz_payment_id).exists():
            return

        try:
            db_order = RazorpayOrder.objects.get(razorpay_order_id=rz_order_id)
        except RazorpayOrder.DoesNotExist:
            return

        amount_inr = payload["amount"] / 100  # paise → INR

        payment = RazorpayPayment.objects.create(
            order=db_order,
            user=db_order.user,
            razorpay_payment_id=rz_payment_id,
            razorpay_signature="",          # signature not available in webhook
            amount=amount_inr,
            currency=payload.get("currency", "INR"),
            method=payload.get("method"),
            status="captured",
            webhook_payload=event,
            paid_at=timezone.now(),
        )

        db_order.status = "paid"
        db_order.save(update_fields=["status"])

        WalletService.credit_wallet(
            payment=payment,
            description="Wallet top-up (webhook fallback)",
        )

    def _handle_payment_failed(self, event: dict):
        payload = event["payload"]["payment"]["entity"]
        rz_order_id = payload.get("order_id")
        if not rz_order_id:
            return

        RazorpayOrder.objects.filter(
            razorpay_order_id=rz_order_id, status="created"
        ).update(status="failed")

