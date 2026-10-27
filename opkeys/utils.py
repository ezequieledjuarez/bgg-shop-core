import hmac
import hashlib
import secrets
from django.conf import settings
from base64 import urlsafe_b64encode
from datetime import timedelta
from django.utils import timezone


def generate_raw_key(length: int = 32) -> str:
    # URL-safe raw key returned to the caller once
    return urlsafe_b64encode(secrets.token_bytes(length)).rstrip(b"=").decode("ascii")


def key_hash(raw_key: str) -> str:
    # HMAC-SHA256 using SECRET_KEY as key
    secret = settings.SECRET_KEY.encode("utf-8")
    dig = hmac.new(secret, raw_key.encode("utf-8"), hashlib.sha256).hexdigest()
    return dig


def default_expiry(minutes: int = 30):
    return timezone.now() + timedelta(minutes=minutes)
