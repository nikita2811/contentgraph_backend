from __future__ import annotations
from rest_framework import serializers
from django.contrib.auth import get_user_model
from datetime import timezone,timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

User = get_user_model()

class RegisterSerializer(serializers.ModelSerializer):
  name = serializers.CharField(max_length=100, required=True, allow_blank=False) 
  password = serializers.CharField(max_length=68,min_length=8,write_only=True,required=True)
  confirm_password =serializers.CharField(max_length=68,min_length=8,write_only=True,required=True)
    

  class Meta:
   model = User
   fields=['name','email','password','confirm_password']
  
  def validate(self, attrs):
          if attrs['password'] != attrs['confirm_password']:
              raise serializers.ValidationError({"password": "Passwords do not match."})
          return attrs
    
  def create(self,validated_data):
   validated_data.pop('confirm_password') 
   return User.objects.create_user(**validated_data)
  
class LoginSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(max_length=255)
    password = serializers.CharField(max_length=68,write_only=True,min_length=8)
    token = serializers.CharField(max_length=255,min_length=8,read_only=True)

    class Meta:
        model=User
        fields = ['email','password','token']  

class UserProfileSerializer(serializers.ModelSerializer):
    avatar_url = serializers.SerializerMethodField()
   
 
    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "name",
            "avatar_url",
            "last_login",
            "credits",
        ]
        read_only_fields = fields


    def get_credits(self, obj):
        wallet = getattr(obj, 'wallet', None)
        return wallet.balance if wallet else 0
 
    
 
    def get_avatar_url(self, obj) -> str | None:
        request = self.context.get("request")
        # If you store avatars on the User model (e.g. via django-avatar or
        # a custom ImageField called `avatar`), swap the line below.
        avatar = getattr(obj, "avatar", None)
        if avatar and hasattr(avatar, "url"):
            return request.build_absolute_uri(avatar.url) if request else avatar.url
        return None