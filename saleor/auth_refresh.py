import hashlib
import hmac
import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional

try:
    from django.http import JsonResponse, HttpRequest
    from django.views.decorators.csrf import csrf_exempt
except Exception:  # pragma: no cover - fallback for test environments without Django
    # Minimal stubs so helpers/tests can run without Django installed.
    class HttpRequest:
        def __init__(self):
            self.method = 'POST'
            self.body = b'{}'

    def JsonResponse(data, status=200):
        # return a simple tuple-like object for tests
        return {'data': data, 'status': status}

    def csrf_exempt(fn):
        return fn

# Minimal in-memory refresh token store for PoC. In production this must be persisted.
_store_lock = threading.Lock()
# maps refresh_token_hash -> (user_id, expires_at)
_refresh_store: Dict[str, Dict] = {}

_ACCESS_TTL = timedelta(seconds=int(os.environ.get('ACCESS_TTL', '300')))
_REFRESH_TTL = timedelta(seconds=int(os.environ.get('REFRESH_TTL', '3600')))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _issue_tokens(user_id: str) -> Dict[str, str]:
    access = f"access-{uuid.uuid4().hex}"
    refresh = f"refresh-{uuid.uuid4().hex}"
    expires = datetime.utcnow() + _REFRESH_TTL
    with _store_lock:
        _refresh_store[_hash_token(refresh)] = {"user_id": user_id, "expires_at": expires}
    return {"access_token": access, "refresh_token": refresh}


def _consume_refresh(refresh_token: str) -> Optional[str]:
    """Validate and rotate refresh token. Returns new refresh token if valid, else None."""
    key = _hash_token(refresh_token)
    with _store_lock:
        rec = _refresh_store.pop(key, None)
        if not rec:
            return None
        if rec["expires_at"] < datetime.utcnow():
            return None
        # rotation: issue new refresh and persist
        new_refresh = f"refresh-{uuid.uuid4().hex}"
        _refresh_store[_hash_token(new_refresh)] = {"user_id": rec["user_id"], "expires_at": datetime.utcnow() + _REFRESH_TTL}
        return new_refresh


@csrf_exempt
def refresh_view(request: HttpRequest):
    if request.method != 'POST':
        return JsonResponse({'detail': 'Method not allowed'}, status=405)
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        payload = {}
    # Accept refresh token in body for simplicity (BFF will call server-side cookie forwarding)
    refresh_token = payload.get('refresh_token')
    if not refresh_token:
        return JsonResponse({'detail': 'refresh_token required'}, status=400)
    new_refresh = _consume_refresh(refresh_token)
    if not new_refresh:
        return JsonResponse({'detail': 'invalid or expired refresh token'}, status=401)
    # For PoC we don't build real JWTs; return opaque access token and rotated refresh
    access = f"access-{uuid.uuid4().hex}"
    return JsonResponse({'access_token': access, 'refresh_token': new_refresh})


# Helpers for tests and other modules
def issue_for_user(user_id: str) -> Dict[str, str]:
    return _issue_tokens(user_id)


def clear_store():
    with _store_lock:
        _refresh_store.clear()
