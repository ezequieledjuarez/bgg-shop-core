import pytest
from graphql import build_schema

from saleor.password_validation import has_required_registration_password_composition

from pathlib import Path


SDL = Path(__file__).resolve().parents[1] / "specs" / "schema.graphql"

PASSWORD_VECTORS = [
    ("Abcdef1!", True),
    ("abcdef1!", False),
    ("ABCDEF1!", False),
    ("Abcdefg!", False),
    ("Abcdefg1", False),
    ("Abcdefg1 ", False),
    ("Abcdefg1\t", False),
    ("Abcdefg1\n", False),
    ("Ábcdef1!", False),
    ("ABCDEF1!ñ", False),
    ("Abcdefg!١", False),
    ("Abcdef1😀", False),
    (" Abcdef1! ", True),
    ("Abcdef1!\né", True),
]


@pytest.mark.parametrize(("password", "expected"), PASSWORD_VECTORS)
def test_registration_password_categories_use_ascii_and_preserve_other_characters(password, expected):
    assert has_required_registration_password_composition(password) is expected


def test_core_contract_documents_composition_only_for_registration():
    schema = build_schema(SDL.read_text(encoding="utf-8"))
    register_password = schema.type_map["RegisterInput"].fields["password"]
    login_password = schema.type_map["LoginInput"].fields["password"]

    assert "ASCII uppercase" in register_password.description
    assert "printable ASCII punctuation" in register_password.description
    assert "not whitespace" in register_password.description
    assert login_password.description is None
