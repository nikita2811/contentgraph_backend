from django.urls import path
from .views import CreateOrderView, VerifyPaymentView, RazorpayWebhookView

urlpatterns = [
    path("billing/order/create/", CreateOrderView.as_view()),
    path("billing/payment/verify/", VerifyPaymentView.as_view()),
    path("billing/webhook/razorpay/", RazorpayWebhookView.as_view()),
]