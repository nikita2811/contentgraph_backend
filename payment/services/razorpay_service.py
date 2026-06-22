import razorpay
import hmac
import hashlib
from django.conf import settings
import os


class RazorpayService:
    def __init__(self):
        self.client = razorpay.Client(
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
        )
        

    def create_order(self, amount_inr: float, receipt: str, notes: dict = None) -> dict:
        """
        Razorpay expects amount in paise (INR × 100).
        Returns the raw Razorpay order dict.
        """
        
        return self.client.order.create({
            "amount": int(amount_inr * 100),
            "currency": "INR",
            "receipt": receipt,
            "notes": notes or {},
        })

    def verify_payment_signature(
        self,
        razorpay_order_id: str,
        razorpay_payment_id: str,
        razorpay_signature: str,
    ) -> bool:
        body = f"{razorpay_order_id}|{razorpay_payment_id}"
        expected = hmac.new(
            settings.RAZORPAY_KEY_SECRET.encode(),
            body.encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, razorpay_signature)

    def verify_webhook_signature(self, payload_body: bytes, signature: str) -> bool:
        expected = hmac.new(
            settings.RAZORPAY_WEBHOOK_SECRET.encode(),
            payload_body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def fetch_payment(self, razorpay_payment_id: str) -> dict:
        return self.client.payment.fetch(razorpay_payment_id)