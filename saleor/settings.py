import os
from pathlib import Path
import environ

# ==============================================================================
# 1. RUTAS BASE DEL PROYECTO & ENTORNO
# ==============================================================================
BASE_DIR = Path(__file__).resolve().parent.parent

# Instancia de environ con esquemas de tipos por defecto
env = environ.Env(
    DEBUG=(bool, True),
    ALLOWED_HOSTS=(list, ["*"]),
)

# Carga de archivo .env opcional si existe en la raíz
environ.Env.read_env(os.path.join(BASE_DIR, ".env"))

# ==============================================================================
# 2. SEGURIDAD Y CONFIGURACIÓN DE RED
# ==============================================================================
SECRET_KEY = env("SECRET_KEY", default="django-insecure-super-secret-key")
DEBUG = env("DEBUG")
MP_ACCESS_TOKEN = env("MP_ACCESS_TOKEN", default="")
MP_SANDBOX = env.bool("MP_SANDBOX", default=False)
MP_CURRENCY_ID = env("MP_CURRENCY_ID", default="")
COUNTRY_MONEY_CODE = env("COUNTRY_MONEY_CODE", default="AR")
FRONTEND_URL = env("FRONTEND_URL", default="http://localhost:3000")

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
OPKEY_INTERNAL_SECRET = env("OPKEY_INTERNAL_SECRET", default="changeme-internal-secret")