import json
import hashlib
import logging
from functools import wraps
from django.http import JsonResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.shortcuts import get_object_or_404

from .models import OpKeyAudit, OpSession
from .utils import has_internal_secret, key_hash
from .utils import generate_raw_key, default_expiry, utc_isoformat
from django.utils import timezone
from django.conf import settings
from django.db import DatabaseError, transaction

logger = logging.getLogger(__name__)


def require_internal_secret(view):
    @wraps(view)
    def guarded(request, *args, **kwargs):
        if not has_internal_secret(request):
            return JsonResponse({"valid": False, "reason": "forbidden"}, status=403)
        return view(request, *args, **kwargs)

    return guarded


def _normalize_op(method: str, path: str) -> str:
    method = (method or "GET").upper()
    # Normalize path to remove querystring
    path_only = path.split("?")[0]
    # Keep as METHOD:PATH
    return f"{method}:{path_only}"


@csrf_exempt
@require_POST
@require_internal_secret
def op_validate(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return HttpResponseBadRequest("invalid json")

    raw_key = payload.get("op_key")
    path = payload.get("path")
    method = payload.get("method", "GET")

    if not raw_key or not path:
        return HttpResponseBadRequest("missing fields")

    h = key_hash(raw_key)
    try:
        session = OpSession.objects.get(key_hash=h)
    except OpSession.DoesNotExist:
        return JsonResponse({"valid": False})

    if not session.is_valid():
        if not session.revoked:
            try:
                with transaction.atomic(using=OpSession.objects.db):
                    session = OpSession.objects.select_for_update().get(pk=session.pk)
                    if not session.revoked and session.is_expired() and not OpKeyAudit.objects.filter(
                        session_id=session.pk, action="EXPIRED"
                    ).exists():
                        OpKeyAudit.objects.create(
                            session_id=session.pk, subject_user_id=session.user_id,
                            actor_user_id=session.user_id, actor_source="KEY_HOLDER", action="EXPIRED",
                        )
            except DatabaseError:
                logger.error("OpKey expiration could not be recorded")
                return JsonResponse({"error": "Operation key expiration could not be confirmed"}, status=503)
        reason = "revoked" if session.revoked else "expiry"
        reference = hashlib.sha256(h.encode("utf-8")).hexdigest()[:16]
        logger.info("OpKey validation denied reason=%s reference=%s", reason, reference)
        return JsonResponse({"valid": False, "reason": "expired_or_revoked"})

    if path.split("?")[0].startswith("/api/v1/admin/"):
        user = session.user if session.user_id else None
        if not user or not user.is_active or not (user.is_staff or user.is_superuser):
            return JsonResponse({"valid": False, "reason": "op_not_allowed"})

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
            "expires_at": utc_isoformat(session.expires_at),
        }
    )


@csrf_exempt
@require_POST
@require_internal_secret
def op_revoke(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return HttpResponseBadRequest("invalid json")

    session_id = payload.get("session_id")
    op_key = payload.get("op_key")
    reason = payload.get("reason", "manual")

    if not session_id and not op_key:
        return HttpResponseBadRequest("provide session_id or op_key")
    if reason not in ("logout", "manual"):
        return HttpResponseBadRequest("invalid reason")

    if op_key:
        h = key_hash(op_key)
        qs = OpSession.objects.filter(key_hash=h)
        reference = hashlib.sha256(h.encode("utf-8")).hexdigest()[:16]
    else:
        qs = OpSession.objects.filter(id=session_id)
        reference = hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:16]

    try:
        with transaction.atomic(using=OpSession.objects.db):
            updated = 0
            for session in qs.select_for_update():
                if session.revoked:
                    continue
                session.revoked = True
                session.save(update_fields=["revoked"])
                OpKeyAudit.objects.create(
                    session_id=session.id, subject_user_id=session.user_id,
                    actor_user_id=session.user_id if reason == "logout" else None,
                    actor_source="KEY_HOLDER" if reason == "logout" else "INTERNAL_SERVICE",
                    action="LOGOUT" if reason == "logout" else "REVOKE",
                )
                updated += 1
    except DatabaseError:
        logger.error("OpKey revocation could not be recorded")
        return JsonResponse({"error": "Operation key revocation could not be confirmed"}, status=503)
    logger.info("OpKey revocation reason=%s reference=%s affected=%s", reason, reference, updated)

    return JsonResponse({"revoked": updated})


@csrf_exempt
@require_POST
@require_internal_secret
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

    try:
        with transaction.atomic(using=OpSession.objects.db):
            sess = OpSession.objects.create(
                user_id=user_id,
                key_hash=h,
                ops=ops,
                expires_at=expires_dt,
            )
            OpKeyAudit.objects.create(
                session_id=sess.id, subject_user_id=sess.user_id, actor_user_id=None,
                actor_source="INTERNAL_SERVICE", action="ISSUE",
            )
    except DatabaseError:
        logger.error("OpKey issuance could not be recorded")
        return JsonResponse({"error": "Operation key issuance could not be confirmed"}, status=503)

    response = JsonResponse({
        "raw_key": raw,
        "session_id": str(sess.id),
        "expires_at": utc_isoformat(sess.expires_at),
    })
    response["Cache-Control"] = "no-store"
    return response
