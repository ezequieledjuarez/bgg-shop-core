from django.conf import settings
from django.utils.deprecation import MiddlewareMixin
from django.contrib.auth import get_user_model
from .models import OpSession
from .utils import key_hash


class OpKeyAuthMiddleware(MiddlewareMixin):
    """Authenticate requests that present a valid X-Op-Key header.

    If a valid OpSession is found and the op allows the requested method+path,
    attach the corresponding user to ``request.user`` so downstream code
    (including GraphQL) treats the request as authenticated.
    """

    def process_request(self, request):
        raw = request.META.get("HTTP_X_OP_KEY") or request.headers.get("X-Op-Key")
        if not raw:
            return None

        # compute hash and try to find session
        try:
            h = key_hash(raw)
            sess = OpSession.objects.filter(key_hash=h).first()
        except Exception:
            return None

        if not sess or not sess.is_valid():
            return None

        # normalize operation and check allowed ops (reuse view helper logic if needed)
        method = (request.method or "GET").upper()
        path = request.path.split("?")[0]
        op = f"{method}:{path}"

        allowed = False
        for allowed_op in sess.ops or []:
            if allowed_op.endswith("*"):
                prefix = allowed_op[:-1]
                if op.startswith(prefix):
                    allowed = True
                    break
            elif allowed_op == op:
                allowed = True
                break

        if not allowed:
            return None

        # touch usage
        try:
            sess.touch()
        except Exception:
            pass

        # attach user if present
        if sess.user_id:
            try:
                User = get_user_model()
                user = User.objects.filter(pk=sess.user_id).first()
                if user:
                    request.user = user
                    # mark source for debugging
                    setattr(request, "op_session", sess)
            except Exception:
                pass

        return None
