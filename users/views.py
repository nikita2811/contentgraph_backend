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
from .models import User
from .utils import send_invite_email
from .utils import (send_verification_email,get_token_for_user,store_refresh_token,
                    is_refresh_token_valid,delete_refresh_token,reset_password_email,verify_password_reset_token)
from django.contrib.auth import authenticate
from rest_framework_simplejwt.views import TokenRefreshView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError, InvalidToken









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
          send_verification_email(user,request)
          return Response(user_data,status=status.HTTP_201_CREATED)
        
        return Response({"errors": serializer_data.errors}, status=status.HTTP_400_BAD_REQUEST)

   


class EmailVerificationView(APIView):

 def get(self,request):
  token = request.GET.get('token')
  try:
    payload = jwt.decode(token,settings.SECRET_KEY,algorithms=['HS256'])
    user=User.objects.get(id=payload['user_id'])
    if not user.is_verified:
     user.is_active = True
     user.is_verified = True
     user.save()
     return Response({'meesage':'Email Verified Successfully'},status=status.HTTP_200_OK)
    else:
      return Response({'message':'User Already Verified'},status=status.HTTP_200_OK)
  except jwt.ExpiredSignatureError:
   return Response({'error':'Activation Expired'},status=status.HTTP_400_BAD_REQUEST)
  except jwt.exceptions.DecodeError:
   return Response({'error':'Invalid Token'},status=status.HTTP_400_BAD_REQUEST)
  
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
                {'error': 'Email is not verified. Please verify your email first.'},
                status=status.HTTP_403_FORBIDDEN
            )
            if not user.is_active:
             return Response(
                {'error': 'Account is disabled. Contact support.'},
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
            print(tokens)
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
  def post(self,request,*args,**kargs):
    refresh_token = request.COOKIES.get('refresh_token')
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
            print(user)
            reset_password_email(user, request)   # send email with token
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
   def get(self,request):
        uid   = request.query_params.get('uid')
        token = request.query_params.get('token')
        
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
        user, error = verify_password_reset_token(uid, token)
        if error:
            return Response(
                {'error': error},
                status=status.HTTP_400_BAD_REQUEST
            )

        #  set new password
        user.set_password(password)
        user.save()

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
        

    

