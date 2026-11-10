import json
import os
from pathlib import Path
import environ
from django.core.exceptions import ImproperlyConfigured

# ==============================================================================
# 1. RUTAS BASE DEL PROYECTO & ENTORNO
# ==============================================================================
BASE_DIR = Path(__file__).resolve().parent.parent

# Instancia de environ con esquemas de tipos por defecto
env = environ.Env(
    DEBUG=(bool, True),
    ALLOWED_HOSTS=(list, ["*"]),
)

# Carga de archivo .env opcional si existe en la raíz.
# SALEOR_SKIP_DOTENV=1 lo omite (tests herméticos que importan estos settings).
if os.environ.get("SALEOR_SKIP_DOTENV") != "1":
    environ.Env.read_env(os.path.join(BASE_DIR, ".env"))

# ==============================================================================
# 2. SEGURIDAD Y CONFIGURACIÓN DE RED
# ==============================================================================
SECRET_KEY = env("SECRET_KEY", default="django-insecure-super-secret-key")
DEBUG = env("DEBUG")
if not DEBUG and (len(SECRET_KEY) < 50 or len(set(SECRET_KEY)) < 5 or SECRET_KEY.startswith("django-insecure-")):
    raise ImproperlyConfigured("A strong SECRET_KEY is required when DEBUG is disabled.")
try:
    SECRET_KEY_FALLBACKS = json.loads(env("SECRET_KEY_FALLBACKS", default="[]"))
except (TypeError, ValueError):
    raise ImproperlyConfigured("SECRET_KEY_FALLBACKS must be a JSON array of nonempty strings.") from None
if not isinstance(SECRET_KEY_FALLBACKS, list) or any(
    not isinstance(key, str) or not key for key in SECRET_KEY_FALLBACKS
):
    raise ImproperlyConfigured("SECRET_KEY_FALLBACKS must be a JSON array of nonempty strings.")
MP_ACCESS_TOKEN = env("MP_ACCESS_TOKEN", default="")
MP_SANDBOX = env.bool("MP_SANDBOX", default=False)
MP_CURRENCY_ID = env("MP_CURRENCY_ID", default="")
COUNTRY_MONEY_CODE = env("COUNTRY_MONEY_CODE", default="AR")
FRONTEND_URL = env("FRONTEND_URL", default="http://localhost:3000")

# ------------------------------------------------------------------------------
# Correo saliente (verificación de email). Por defecto: captura local (Mailpit).
# Un host SMTP externo exige EMAIL_EXTERNAL_SMTP_ENABLED=true, TLS/SSL y un
# DEFAULT_FROM_EMAIL explícito. La contraseña solo se lee desde el entorno.
# ------------------------------------------------------------------------------
_email_url = env.email_url("EMAIL_URL", default="smtp://mailpit:1025")
EMAIL_BACKEND = _email_url["EMAIL_BACKEND"]
EMAIL_HOST = env("EMAIL_HOST", default=_email_url.get("EMAIL_HOST") or "mailpit")
EMAIL_PORT = env.int("EMAIL_PORT", default=_email_url.get("EMAIL_PORT") or 1025)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default=_email_url.get("EMAIL_HOST_USER") or "")
EMAIL_HOST_PASSWORD = env(
    "MEEPLE_APP_SMTP_PASSWORD",
    default=env("EMAIL_HOST_PASSWORD", default=_email_url.get("EMAIL_HOST_PASSWORD") or ""),
)
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=bool(_email_url.get("EMAIL_USE_TLS")))
EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL", default=bool(_email_url.get("EMAIL_USE_SSL")))
EMAIL_TIMEOUT = 3
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="noreply@saleor.local")
EMAIL_EXTERNAL_SMTP_ENABLED = env.bool("EMAIL_EXTERNAL_SMTP_ENABLED", default=False)
EMAIL_VERIFICATION_FRONTEND_ORIGIN = env(
    "EMAIL_VERIFICATION_FRONTEND_ORIGIN", default=FRONTEND_URL
)
if EMAIL_USE_TLS and EMAIL_USE_SSL:
    raise ImproperlyConfigured("EMAIL_USE_TLS and EMAIL_USE_SSL are mutually exclusive.")
if EMAIL_BACKEND.endswith("smtp.EmailBackend") and EMAIL_HOST not in (
    "mailpit", "localhost", "127.0.0.1", "::1"
):
    if not EMAIL_EXTERNAL_SMTP_ENABLED:
        raise ImproperlyConfigured(
            "An external SMTP host requires EMAIL_EXTERNAL_SMTP_ENABLED=true."
        )
    if not (EMAIL_USE_TLS or EMAIL_USE_SSL) or DEFAULT_FROM_EMAIL == "noreply@saleor.local":
        raise ImproperlyConfigured(
            "An external SMTP host requires EMAIL_USE_TLS or EMAIL_USE_SSL and an explicit "
            "DEFAULT_FROM_EMAIL."
        )

# Permite cualquier Host en desarrollo para evitar el HTTP 400 Bad Request
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["*"])

# Configuración de CORS para autorizar las solicitudes desde Flask (BFF) y React
CORS_ALLOWED_ORIGINS = env.list(
    "CORS_ALLOWED_ORIGINS", 
    default=["http://localhost:3000", "http://localhost:5000"]
)
CORS_ALLOW_ALL_ORIGINS = DEBUG

CSRF_TRUSTED_ORIGINS = env.list(
    "CSRF_TRUSTED_ORIGINS",
    default=["http://localhost:3000", "http://localhost:5000"]
)

# Desactiva redirecciones automáticas para asegurar el procesamiento de POST /graphql/
APPEND_SLASH = False

# ==============================================================================
# 3. APLICACIONES REGISTRADAS (INSTALLED_APPS)
# ==============================================================================
INSTALLED_APPS = [
    # Framework Core de Django
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    
    # Librerías de Terceros
    "corsheaders",
    "graphene_django",
    
    # Dominio de Saleor Core (Módulos activos)
    "saleor.graphql",
    "saleor.promotions",
    "saleor.catalog_events",
    # Operations & Keys app for short-lived op sessions
    "opkeys",
]

# ==============================================================================
# 4. PIPELINE DE MIDDLEWARES
# ==============================================================================
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",  # Intercepta y autoriza solicitudes pre-flight
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # OpKeys middleware: accept X-Op-Key and set request.user when valid
    "opkeys.middleware.OpKeyAuthMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

# ==============================================================================
# 5. ENRUTAMIENTO PRINCIPAL
# ==============================================================================
ROOT_URLCONF = "saleor.urls"

# ==============================================================================
# 6. PERSISTENCIA DE DATOS (PostgreSQL - OLTP)
# ==============================================================================
DATABASES = {
    "default": env.db(
        "DATABASE_URL", 
        default="postgresql://saleor:saleor_pass@db:5432/saleor"
    )
}

# ==============================================================================
# 7. TAREAS ASÍNCRONAS Y CACHÉ (Celery + Redis)
# ==============================================================================
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://redis:6379/1")
CELERY_RESULT_BACKEND = env("REDIS_URL", default="redis://redis:6379/0")
CELERY_TIMEZONE = "UTC"
CELERY_ENABLE_UTC = True
CELERY_BEAT_SCHEDULE = {
    "dispatch-catalog-events": {
        "task": "saleor.catalog_events.tasks.dispatch_due_catalog_events",
        "schedule": 5.0,
    },
}
CATALOG_EVENTS_BFF_URL = env(
    "CATALOG_EVENTS_BFF_URL",
    default="http://bff:5000/internal/v1/catalog-events",
)

# ==============================================================================
# 8. MOTOR DE GRAPHQL (Graphene)
# ==============================================================================
GRAPHENE = {
    "SCHEMA": "saleor.graphql.api.schema",
    "MIDDLEWARE": [
        "graphene_django.debug.DjangoDebugMiddleware",
    ],
}

# ==============================================================================
# 9. ARCHIVOS ESTÁTICOS Y MEDIA
# ==============================================================================
STATIC_URL = "/static/"
STATIC_ROOT = os.path.join(BASE_DIR, "static")

MEDIA_URL = "/media/"
MEDIA_ROOT = env("MEDIA_ROOT", default=os.path.join(BASE_DIR, "media"))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Feature flags and secrets for OpKeys
OPKEYS_ISSUE_ON_LOGIN = env.bool("OPKEYS_ISSUE_ON_LOGIN", default=False)
# Secret header value required by internal endpoints (BFF -> Core)
OPKEY_INTERNAL_SECRET = env("OPKEY_INTERNAL_SECRET", default="")
CHAT_ASSERTION_SECRET = env("CHAT_ASSERTION_SECRET", default="")
CATALOG_EVENTS_HMAC_SECRET = env(
    "CATALOG_EVENTS_HMAC_SECRET",
    default=OPKEY_INTERNAL_SECRET,
)