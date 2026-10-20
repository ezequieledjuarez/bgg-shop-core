from django.urls import path
from django.views.decorators.csrf import csrf_exempt
from graphene_django.views import GraphQLView

from .graphql.orders import invoice_pdf

urlpatterns = [
    path("invoices/<uuid:order_id>/", invoice_pdf, name="invoice-pdf"),
    # Endpoint principal que procesa consultas GraphQL
    path("graphql/", csrf_exempt(GraphQLView.as_view(graphiql=True))),
]