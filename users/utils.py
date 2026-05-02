import jwt
from django.conf import settings
from django.core.mail import EmailMessage
from django.contrib.sites.shortcuts import get_current_site
from django.urls import reverse
from rest_framework_simplejwt.tokens import RefreshToken
from django.core.cache import cache
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils.encoding import force_str, force_bytes        # ✅ NO smart_bytes
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.contrib.auth import get_user_model
import logging
from upstash_redis import Redis
import os
from .email import send_html_email
from rest_framework.response import Response

r = Redis(
    url=os.environ.get("UPSTASH_REDIS_REST_URL"),  # paste directly
    token= os.environ.get("UPSTASH_REDIS_REST_TOKEN")                  # paste directly
)



User = get_user_model()

logger = logging.getLogger(__name__) 
User = get_user_model()


REFRESH_TOKEN_EXPIRY = 60 * 60 * 24 * 7


def store_refresh_token(user_id, refresh_token): 
    key = f'refresh_token:{user_id}'
    r.set(key, refresh_token, ex=REFRESH_TOKEN_EXPIRY)


def get_refresh_token(user_id):
    key = f'refresh_token:{user_id}'
    return r.get(key)


def delete_refresh_token(user_id):
    key = f'refresh_token:{user_id}'
    r.delete(key)


def is_refresh_token_valid(user_id, token):
    stored = get_refresh_token(user_id)          # ✅ correct function name
    return stored == token


def get_token_for_user(user):                   # ✅ renamed from get_token_for_user(self)
    refresh = RefreshToken.for_user(user)
    refresh['email'] = user.email

    return {
        'refresh': str(refresh),
        'access':  str(refresh.access_token)
    }


def send_verification_email(user, request):
    token  = str(RefreshToken.for_user(user).access_token)  # ✅ access token only
    domain = get_current_site(request).domain
    link   = reverse('verify-email')

    verification_url=(
        f'http://{domain}{link}?token={token}'
    )
    context = {
            "user": user,
            "verification_url": verification_url,
            "expiry_time": "24 hours"
        }
    send_html_email(
            subject="Verify Your Email",
            template_name="verify_email.html",
            context=context,
            recipient_list=[user.email]
        )

    return Response({
            "message": "Verification email sent"
        })
    


def generate_password_reset_token(user):
    token   = PasswordResetTokenGenerator().make_token(user)
    user_id = urlsafe_base64_encode(force_bytes(user.pk))   # ✅ force_bytes + user.pk
    return user_id, token


def reset_password_email(user, request):
    uid, token = generate_password_reset_token(user)
    domain     = get_current_site(request).domain
    link       = reverse('reset-password')
    url        = f'http://{domain}{link}?uid={uid}&token={token}'

    email = EmailMessage(
        subject = 'Reset your Password',
        body    = f'Hi {user.name},\n\nClick the link below to reset your password:\n{url}',
        to      = [user.email],
    )
    email.send()                                             # ✅ was commented out


def verify_password_reset_token(uid, token):
    try:
        user_id = force_str(urlsafe_base64_decode(uid))
        print(f"decoded user_id = {user_id}")
        user    = User.objects.get(pk=user_id)

        if not PasswordResetTokenGenerator().check_token(user, token):
            return None, 'Token is invalid or has expired.'

        return user, None

    except User.DoesNotExist:
        return None, 'User not found.'
    except Exception:
        return None, 'Invalid token.'
    



