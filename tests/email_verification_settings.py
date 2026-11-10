"""Hermetic Django settings for email-verification tests.

Independent of saleor.settings: no .env file, no environment lookups, no
application database. PostgreSQL tests swap in a guarded disposable database.
"""

SECRET_KEY = "email-verification-test-signing-key-0123456789abcdef"
DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]
USE_TZ = True
TIME_ZONE = "UTC"
ROOT_URLCONF = "saleor.urls"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "graphene_django",
    "saleor.graphql",
    "opkeys",
]

MIDDLEWARE = []

DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
}

GRAPHENE = {"SCHEMA": "saleor.graphql.api.schema"}

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
DEFAULT_FROM_EMAIL = "noreply@example.test"
EMAIL_TIMEOUT = 3
FRONTEND_URL = "http://frontend.example.test"
EMAIL_VERIFICATION_FRONTEND_ORIGIN = "http://frontend.example.test"

OPKEY_INTERNAL_SECRET = "test-internal-secret-0123456789abcdef0123456789"
OPKEYS_ISSUE_ON_LOGIN = False
CATALOG_EVENTS_BFF_URL = "http://bff.invalid/internal/v1/catalog-events"
