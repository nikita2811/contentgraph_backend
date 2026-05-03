from django.db import models
from django.contrib.auth.models import (BaseUserManager,AbstractBaseUser,PermissionsMixin)
from django.utils import timezone
import uuid

# Create your models here.
class UserManager(BaseUserManager):

    def create_user(self,email,password=None,**extra_fields):
        if not email:
            raise TypeError("Email not Provided")
        
        user = self.model(email=self.normalize_email(email),**extra_fields)

        if password:
            user.set_password(password)
        user.save()

        return user
    
    def create_superuser(self,email,password,**extra_fields):
         user = self.create_user(email,password,**extra_fields)
         user.is_active = True
         user.superuser = True
         user.is_verified = True
       
         user.save()
       
         return user

class User(AbstractBaseUser,PermissionsMixin):
     name = models.CharField(max_length=68)
     email = models.EmailField(max_length=255,unique=True,db_index=True)
     is_verified = models.BooleanField(default=False)
     is_active =models.BooleanField(default=False)
     is_staff=models.BooleanField(default=False)


     USERNAME_FIELD='email'
     objects = UserManager()

     def __str__(self):
      return self.email
    


class PasswordResetToken(models.Model):
    user       = models.ForeignKey(User, on_delete=models.CASCADE)
    token      = models.UUIDField(default=uuid.uuid4, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_used    = models.BooleanField(default=False)

    def is_expired(self):
        # expires after 30 minutes
        return timezone.now() > self.created_at + timezone.timedelta(minutes=30)

    def __str__(self):
        return f"{self.user.email} - {self.token}"