from django.urls import path
from .views import (
    RegisterView,
    EmailVerificationView,
    ResendVerificationEmailView,
    LoginView,
    LogoutView,
    RedisTokenRefreshView,
    ResetPassword,
    NewResetPassword,
    
   
)

urlpatterns=[
    path('signup',RegisterView.as_view(),name='register'),
    path('verify-email',EmailVerificationView.as_view(),name='verify-email'),
    path('resend-verify-email',ResendVerificationEmailView.as_view(),name='resend-email'),
    path('signin',LoginView.as_view(),name='login'),
    path('forgot-password',ResetPassword.as_view(),name='forgot-password'),
    path('reset-password',NewResetPassword.as_view(),name='reset-password'),
    path('token/refresh',RedisTokenRefreshView.as_view(),name='token-refresh'),
    path('logout',LogoutView.as_view(),name='logout'),

 
]