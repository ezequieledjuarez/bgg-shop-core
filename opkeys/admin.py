from django.contrib import admin
from .models import OpSession


@admin.register(OpSession)
class OpSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "issued_at", "expires_at", "revoked")
    search_fields = ("id", "user__username", "description")
    list_filter = ("revoked",)
