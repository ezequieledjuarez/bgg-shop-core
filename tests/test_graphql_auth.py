import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
django.setup()

from saleor.graphql.api import schema


def test_authentication_schema_exposes_contract():
    mutation = str(schema)
    assert "register(input: RegisterInput!): RegisterPayload!" in mutation
    assert "login(input: LoginInput!): LoginPayload!" in mutation
    assert "token: String" in mutation
    assert "refreshToken: String" in mutation


def test_authentication_schema_does_not_expose_passwords():
    document = str(schema)
    user_type = document.split("type User {", 1)[1].split("}", 1)[0]
    assert "password" not in user_type.lower()
