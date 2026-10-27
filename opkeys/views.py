import json
from django.http import JsonResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.shortcuts import get_object_or_404

from .models import OpSession
from .utils import key_hash
from .utils import generate_raw_key, default_expiry
from django.utils import timezone
from django.conf import settings


def _normalize_op(method: str, path: str) -> str:
    method = (method or "GET").upper()
    # Normalize path to remove querystring
    path_only = path.split("?")[0]
    # Keep as METHOD:PATH
    return f"{method}:{path_only}"


@csrf_exempt
@require_POST
def op_validate(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return HttpResponseBadRequest("invalid json")

    raw_key = payload.get("op_key")
    path = payload.get("path")
    method = payload.get("method", "GET")

    # Security: require internal secret header to be present and correct
    secret = request.headers.get("X-Internal-Secret") or request.META.get("HTTP_X_INTERNAL_SECRET")
    if not secret or secret != getattr(settings, "OPKEY_INTERNAL_SECRET", None):
        return JsonResponse({"valid": False, "reason": "forbidden"}, status=403)

    if not raw_key or not path:
        return HttpResponseBadRequest("missing fields")

    h = key_hash(raw_key)
    try:
        session = OpSession.objects.get(key_hash=h)
    except OpSession.DoesNotExist:
        return JsonResponse({"valid": False})

    if not session.is_valid():
        return JsonResponse({"valid": False, "reason": "expired_or_revoked"})

    op = _normalize_op(method, path)
    # Check exact match or prefix match
    allowed = False
    for allowed_op in session.ops:
        if allowed_op.endswith("*"):
            prefix = allowed_op[:-1]
            if op.startswith(prefix):
                allowed = True
                break
        elif allowed_op == op:
            allowed = True
            break

    if not allowed:
        return JsonResponse({"valid": False, "reason": "op_not_allowed"})

    # Touch last_used
    session.touch()

    return JsonResponse(
        {
            "valid": True,
            "session_id": str(session.id),
            "user_id": session.user_id,
            "ops": session.ops,
            "expires_at": session.expires_at.isoformat() if session.expires_at else None,
        }
    )


@csrf_exempt
@require_POST
def op_revoke(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return HttpResponseBadRequest("invalid json")

    session_id = payload.get("session_id")
    op_key = payload.get("op_key")

    if not session_id and not op_key:
        return HttpResponseBadRequest("provide session_id or op_key")

    if op_key:
        h = key_hash(op_key)
        qs = OpSession.objects.filter(key_hash=h)
    else:
        qs = OpSession.objects.filter(id=session_id)

    updated = qs.update(revoked=True)

    return JsonResponse({"revoked": updated})


@csrf_exempt
@require_POST
def op_create(request):
    """Create an OpSession and return the raw key once.

    Body: { "ops": ["GET:/graphql", "POST:/internal/*"], "user_id": <optional> }
    Returns: { "raw_key": "...", "session_id": "...", "expires_at": "..." }
    """
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return HttpResponseBadRequest("invalid json")

    ops = payload.get("ops") or []
    user_id = payload.get("user_id")
    expires_at = payload.get("expires_at")

    raw = generate_raw_key()
    h = key_hash(raw)

    # Compute expiry
    if expires_at:
        try:
            expires_dt = timezone.datetime.fromisoformat(expires_at)
        except Exception:
            expires_dt = default_expiry()
    else:
        expires_dt = default_expiry()

    sess = OpSession.objects.create(
        user_id=user_id,
        key_hash=h,
        ops=ops,
        expires_at=expires_dt,
    )

    return JsonResponse({
        "raw_key": raw,
        "session_id": str(sess.id),
        "expires_at": sess.expires_at.isoformat() if sess.expires_at else None,
    })
