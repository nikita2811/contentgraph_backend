from django.urls import path
from .views import CreateOrderView, VerifyPaymentView, RazorpayWebhookView

urlpatterns = [
    path("/order/create", CreateOrderView.as_view()),
    path("/order/verify", VerifyPaymentView.as_view()),
    path("/webhook/razorpay", RazorpayWebhookView.as_view()),
]