from django.urls import path
from .views import (CreateOrderView, VerifyPaymentView, 
                    RazorpayWebhookView,WalletView,TransactionListView,PlanView,BalanceCheckView)

urlpatterns = [
    path("order/create", CreateOrderView.as_view()),
    path("order/verify", VerifyPaymentView.as_view()),
    path("webhook/razorpay", RazorpayWebhookView.as_view()),
    path('wallet', WalletView.as_view(), name='wallet'),
    path('transactions', TransactionListView.as_view(), name='transactions'),
    path('create-plan',PlanView.as_view()),
    path('balance-check', BalanceCheckView.as_view(), name='balance-check'),
]