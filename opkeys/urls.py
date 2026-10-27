from django.urls import path
from . import views

urlpatterns = [
    path("op-create", views.op_create, name="op-create"),
    path("op-validate", views.op_validate, name="op-validate"),
    path("op-revoke", views.op_revoke, name="op-revoke"),
]
