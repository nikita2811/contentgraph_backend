import razorpay
from django.conf import settings
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse

client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

def create_order(request):
    amount = 50000  # in paise (₹500.00)
    order = client.order.create({
        "amount": amount,
        "currency": "INR",
        "payment_capture": 1  # auto-capture
    })
    context = {
        "order_id": order["id"],
        "amount": amount,
        "razorpay_key": settings.RAZORPAY_KEY_ID,
    }
    return render(request, "payment.html", context)

@csrf_exempt
def verify_payment(request):
    if request.method == "POST":
        data = request.POST
        try:
            client.utility.verify_payment_signature({
                "razorpay_order_id":   data["razorpay_order_id"],
                "razorpay_payment_id": data["razorpay_payment_id"],
                "razorpay_signature":  data["razorpay_signature"],
            })
            # ✅ Payment verified — update your DB here
            return JsonResponse({"status": "success"})
        except razorpay.errors.SignatureVerificationError:
            return JsonResponse({"status": "failed"}, status=400)
