from rest_framework import serializers
from .models import UserWallet,WalletTransaction

class WalletSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserWallet
        fields = ['balance', 'total_credited', 'total_debited', 'currency', 'updated_at']





class WalletTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = WalletTransaction
        fields = [
            'id',
            'transaction_type',
            'amount',
            'balance_before',
            'balance_after',
            'description',
            'created_at',
        ]