from django import forms
from django.db.models import fields
from .models import YQPoint_Distribute


class UserForm(forms.Form):
    username = forms.CharField(label="username", max_length=128)
    password = forms.CharField(
        label="password", max_length=256, widget=forms.PasswordInput
    )


class YQPoint_DistributionForm(forms.ModelForm):
    class Meta:
        model = YQPoint_Distribute
        exclude = []