from django.urls import path, include
from django.views.decorators.csrf import csrf_exempt
from graphene_django.views import GraphQLView

from .graphql.orders import invoice_pdf
from .auth_refresh import refresh_view

urlpatterns = [
    path("invoices/<uuid:order_id>/", invoice_pdf, name="invoice-pdf"),
    # Endpoint principal que procesa consultas GraphQL
    path("graphql/", csrf_exempt(GraphQLView.as_view(graphiql=True))),
    # Minimal refresh endpoint used by BFF to rotate refresh tokens
    path("api/v1/auth/refresh", csrf_exempt(refresh_view), name="auth-refresh"),
    # Internal endpoints for lightweight services (BFF)
    path("internal/", include(("opkeys.urls", "opkeys"), namespace="opkeys")),
]