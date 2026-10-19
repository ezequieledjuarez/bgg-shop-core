from django.urls import path
from django.views.decorators.csrf import csrf_exempt
from graphene_django.views import GraphQLView

urlpatterns = [
    # Endpoint principal que procesa consultas GraphQL
    path("graphql/", csrf_exempt(GraphQLView.as_view(graphiql=True))),
]