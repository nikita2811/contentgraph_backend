from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny
from .serializers import (RegisterSerializer,LoginSerializer)
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.sites.shortcuts import get_current_site
from django.conf import settings
import jwt
import logging
from .models import User,PasswordResetToken
from .utils import (send_verification_email,get_token_for_user,store_refresh_token,
                    is_refresh_token_valid,delete_refresh_token,reset_password_email)
from django.contrib.auth import authenticate
from rest_framework_simplejwt.views import TokenRefreshView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError, InvalidToken
from django.utils.http import urlsafe_base64_decode
from django.utils.encoding import force_str
from django.contrib.auth.tokens import default_token_generator
from django.shortcuts import redirect
import threading










# Create your views here.
logger = logging.getLogger(__name__) 
User = get_user_model()

class RegisterView(APIView):
    permission_classes = [AllowAny] 
    serializer = RegisterSerializer

    def post(self,request):
        user = request.data
        serializer_data = self.serializer(data=user)
        logger.info(serializer_data)
        if serializer_data.is_valid():
          serializer_data.save()
          user_data = serializer_data.data
          user = User.objects.get(email=user_data['email'])
          logger.info("code working fine till here")
          # Send email in background thread (don't block response)
          thread = threading.Thread(target=send_verification_email, args=(user,request))
          thread.start()
       
          logger.info("code working fine till here 2")
          return Response(user_data,status=status.HTTP_201_CREATED)
        
        return Response({"errors": serializer_data.errors}, status=status.HTTP_400_BAD_REQUEST)

   


class EmailVerificationView(APIView):

 def get(self,request):
  token = request.GET.get('token')
  uid = request.GET.get('uid')
  FRONTEND = settings.FRONTEND_URL
  try:
            uid  = force_str(urlsafe_base64_decode(uid))  # ← decode here
            user = User.objects.get(pk=uid)
  except (User.DoesNotExist, ValueError, TypeError):
            return redirect(f'{FRONTEND}/signin?status=error&message=Invalid verification link')

  if default_token_generator.check_token(user, token):
            user.is_active = True
            user.is_verified = True
            user.save()
            return redirect(f'{FRONTEND}/signin?status=success&message=Email verified successfully')

  return redirect(f'{FRONTEND}/verify-email?status=error&message=Link expired or already used')
  
class ResendVerificationEmailView(APIView):
 def post(self,request):
  email_data = request.data.get('email') 
  try:
    user = User.objects.get(email__exact=email_data)
    send_verification_email(user,request)
    return Response({'message':'Verification Email sent,check your inbox'},status=status.HTTP_200_OK)
  except User.DoesNotExist:
   return Response({'error':'User does not exist'},status=status.HTTP_400_BAD_REQUEST)
  

class LoginView(APIView):
    serializer_class = LoginSerializer
    def post(self, request):
        email    = request.data.get('email')
        password = request.data.get('password')
        serializer = self.serializer_class(data=request.data)
        if serializer.is_valid(raise_exception=True):
            if not email or not password:
             return Response(
                {'error': 'Email and password are required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
            try:
             user = User.objects.get(email=email)
            except User.DoesNotExist:
             return Response(
                {'error': 'Invalid email or password.'},
                status=status.HTTP_401_UNAUTHORIZED
            )
            if not user.is_verified:
             return Response(
                {'code': 'EMAIL_NOT_VERIFIED',
                'error': 'Email is not verified. Please verify your email first.'},
                status=status.HTTP_403_FORBIDDEN
            )
            if not user.is_active:
             return Response(
                {'code': 'ACCOUNT_DISABLED',
                'error': 'Account is disabled. Contact support.'},
                status=status.HTTP_403_FORBIDDEN
            )

            user = authenticate(email=email, password=password)
            if not user:
             logger.warning(f"Failed login attempt for email: {email}")
             return Response(
                {'error': 'Invalid email or password.'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # 6. Generate JWT tokens
            tokens = get_token_for_user(user)
            refresh_token= str(tokens['refresh'])
            store_refresh_token(user.id, refresh_token)
            logger.info(f"User logged in: {email}")
            response = Response({
            'message': 'Login successful.',
            'tokens':  tokens,
            'user': {
                'id':       user.id,
                'email':    user.email,
            }
        }, status=status.HTTP_200_OK)
            response.set_cookie(
            key      = 'refresh_token',
            value    = refresh_token,
            httponly = True,             # JS cannot access this cookie
            secure   = False,            # HTTPS only — set False in development
            samesite = 'Lax',           # CSRF protection
            max_age  = 60 * 60 * 24 * 7 # 7 days in seconds
        )

        return response
            
        
    
class RedisTokenRefreshView(TokenRefreshView):
  def post(self,request):
    refresh_token = request.data.get('refresh') or request.COOKIES.get('refresh_token')
    if not refresh_token:
            return Response(
                {'error':'Refresh token not found. Please login again.'},
                status=status.HTTP_400_BAD_REQUEST
            )
    request.data['refresh'] = refresh_token
    try:
        token   = RefreshToken(refresh_token)
        user_id = token['user_id']
    except TokenError as e:
            return Response(
                {'error': 'Invalid or expired refresh token.'},
                status=status.HTTP_401_UNAUTHORIZED
            )
    if not is_refresh_token_valid(user_id, refresh_token):
            return Response(
                {'error': 'Refresh token not recognised. Please login again.'},
                status=status.HTTP_401_UNAUTHORIZED
            )
    serializer = self.get_serializer(data=request.data)
    try:
            serializer.is_valid(raise_exception=True)
    except TokenError as e:
            raise InvalidToken(e.args[0])

    new_refresh = serializer.validated_data.get('refresh', refresh_token)
    new_access  = serializer.validated_data['access']
    store_refresh_token(user_id, new_refresh)

    return Response({
            'message': 'Token refreshed successfully.',
            'access':  new_access,
            'refresh': new_refresh,
        }, status=status.HTTP_200_OK)
  
class ResetPassword(APIView):
   def post(self,request):
      email = request.data.get('email')
      if not email:
         return Response({'error':'Email Required'},status=status.HTTP_400_BAD_REQUEST)
      user = User.objects.filter(email = email).get()
      if not user:
         return Response({'error':'User not found'},status=status.HTTP_400_BAD_REQUEST)
      try:
            user = User.objects.get(email=email)
            # Create new token
            reset_token = PasswordResetToken.objects.create(user=user)
            reset_password_email(user,reset_token.token)   # send email with token
            return Response(
                {'message': 'Password reset email sent. Check your inbox.'},
                status=status.HTTP_200_OK
            )
      except User.DoesNotExist:
            #  vague on purpose — don't reveal if email exists
            return Response(
                {'message': 'Password reset email sent. Check your inbox.'},
                status=status.HTTP_200_OK
            )
class NewResetPassword(APIView):
   def post(self,request):
        token = request.data.get('token')
        password = request.data.get('password')
        confirm_password = request.data.get('confirm_password')


        if not all([ token, password, confirm_password]):
            return Response(
                {'error': ' token, password and confirm_password are required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if password != confirm_password:
            return Response(
                {'error': 'Passwords do not match.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            reset_token = PasswordResetToken.objects.get(token=token, is_used=False)
        except PasswordResetToken.DoesNotExist:
            return Response({"error": "Invalid or expired token"}, status=400)

        # Check expiry
        if reset_token.is_expired():
            reset_token.delete()
            return Response({"error": "Token expired, please request a new one"}, status=400)

         # Reset password
        user = reset_token.user
        user.set_password(password)
        user.save()

        # Invalidate token after use ✅
        reset_token.is_used = True
        reset_token.save()
       

        return Response(
            {'message': 'Password reset successfully. You can now login.'},
            status=status.HTTP_200_OK
        )

class LogoutView(APIView):

    def post(self, request):
        refresh_token = request.COOKIES.get('refresh_token')

        if not refresh_token:
            return Response(
                {'error': 'Refresh token is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        try:
            token   = RefreshToken(refresh_token)
            user_id = token['user_id']

    
            delete_refresh_token(user_id)
            token.blacklist()

            return Response(
                {'message': 'Logged out successfully.'},
                status=status.HTTP_200_OK
            )
        except TokenError:
            return Response(
                {'error': 'Invalid token.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        

    

