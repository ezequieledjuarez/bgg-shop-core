import json
import re
import uuid

from django.conf import settings
from django.http import JsonResponse
from django.utils.deprecation import MiddlewareMixin
from django.contrib.auth import get_user_model
from graphql import GraphQLError, get_operation_ast, get_variable_values, parse, validate
from graphql.execution.values import get_argument_values
from graphql.language import FieldNode, FragmentDefinitionNode, FragmentSpreadNode, InlineFragmentNode, OperationType
from .models import OpSession
from .utils import has_internal_secret, key_hash


def _delegated_operation_allowed(request, method, path):
    if request.content_type != "application/json" or request.GET:
        return False
    try:
        routes = [
            ("GET", r"/api/v1/admin/op-sessions", OperationType.QUERY, "adminOpSessions", None),
            ("POST", r"/api/v1/admin/op-sessions/(?P<sessionId>[^/]+)/(?P<action>revoke|expire)", OperationType.MUTATION, "manageOpSession", "session"),
            ("GET", r"/api/v1/admin/audit", OperationType.QUERY, "adminAuditLogs", None),
            ("GET", r"/api/v1/admin/sales-metrics", OperationType.QUERY, "adminSalesMetrics", None),
            ("GET", r"/api/v1/admin/sales-metrics/categories", OperationType.QUERY, "adminSalesCategories", None),
            ("GET", r"/api/v1/admin/coupons", OperationType.QUERY, "adminCoupons", None),
            ("POST", r"/api/v1/admin/coupons", OperationType.MUTATION, "createAdminCoupon", None),
            ("PATCH", r"/api/v1/admin/coupons/(?P<couponId>[0-9]+)", OperationType.MUTATION, "updateAdminCoupon", "couponId"),
            ("POST", r"/api/v1/admin/gift-cards", OperationType.MUTATION, "createGiftCard", None),
            ("POST", r"/api/v1/admin/products", OperationType.MUTATION, "createProduct", None),
            ("GET", r"/api/v1/admin/products/(?P<bggId>[0-9]+)", OperationType.QUERY, "productByBggId", "bggId"),
            ("PUT", r"/api/v1/admin/products/(?P<id>[^/]+)", OperationType.MUTATION, "updateProduct", "id"),
            ("POST", r"/api/v1/admin/products/(?P<productId>[0-9]+)/stock", OperationType.MUTATION, ("addProductStock", "adjustProductStock"), "productId"),
            ("GET", r"/api/v1/admin/stock-movements/(?P<movementId>[^/]+)", OperationType.QUERY, "adminStockMovementStatus", "movementId"),
            ("GET", r"/api/v1/admin/promotions", OperationType.QUERY, "promotions", None),
            ("POST", r"/api/v1/admin/promotions", OperationType.MUTATION, "createPromotion", None),
            ("PUT", r"/api/v1/admin/promotions/(?P<id>[^/]+)", OperationType.MUTATION, "updatePromotion", "id"),
            ("POST", r"/api/v1/admin/coupons/(?P<code>[^/]+)/apply", OperationType.MUTATION, "applyCouponPreview", "code"),
        ]
        route = next(((match, operation_type, field, binding)
                      for route_method, pattern, operation_type, field, binding in routes
                      if route_method == method and (match := re.fullmatch(pattern, path))), None)
        if route is None:
            return False
        match, operation_type, field_name, binding = route
        payload = json.loads(request.body.decode("utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("query"), str):
            return False
        operation_name = payload.get("operationName")
        if operation_name is not None and not isinstance(operation_name, str):
            return False
        if payload.get("variables") is not None and not isinstance(payload["variables"], dict):
            return False
        document = parse(payload["query"])
        from saleor.graphql.api import schema
        if validate(schema.graphql_schema, document):
            return False
        operation = get_operation_ast(document, None if operation_name in (None, "", "null") else operation_name)
        if operation is None or operation.operation != operation_type:
            return False
        variables = get_variable_values(schema.graphql_schema, operation.variable_definitions, payload.get("variables") or {})
        if not isinstance(variables, dict):
            return False
        fragments = {definition.name.value: definition for definition in document.definitions
                     if isinstance(definition, FragmentDefinitionNode)}
        pending = list(operation.selection_set.selections)
        fields = []
        while pending:
            selection = pending.pop()
            if isinstance(selection, FieldNode):
                fields.append(selection)
            elif isinstance(selection, InlineFragmentNode):
                pending.extend(selection.selection_set.selections)
            elif isinstance(selection, FragmentSpreadNode):
                pending.extend(fragments[selection.name.value].selection_set.selections)
            else:
                return False
        allowed_fields = (field_name,) if isinstance(field_name, str) else field_name
        if len(fields) != 1 or fields[0].name.value not in allowed_fields:
            return False
        field_name = fields[0].name.value
        root_type = schema.graphql_schema.query_type if operation_type == OperationType.QUERY else schema.graphql_schema.mutation_type
        field = root_type.fields[field_name]
        arguments = get_argument_values(field, fields[0], variables)
        if binding == "session":
            session_key = field.args["sessionId"].out_name or "sessionId"
            return (uuid.UUID(str(arguments.get(session_key))) == uuid.UUID(match.group("sessionId"))
                    and arguments.get("action") == match.group("action").upper())
        argument_name = "id" if binding == "couponId" else binding
        argument_key = (field.args[argument_name].out_name or argument_name) if binding is not None else None
        if binding == "bggId":
            return arguments.get(argument_key) == int(match.group(binding))
        if binding == "productId":
            identifier = arguments.get(argument_key)
            return (isinstance(identifier, str) and identifier.isascii() and identifier.isdecimal()
                    and bool(identifier.lstrip("0"))
                    and identifier.lstrip("0") == match.group(binding).lstrip("0"))
        if binding == "couponId":
            identifier = arguments.get(argument_key)
            return type(identifier) is int and identifier == int(match.group(binding))
        if binding == "movementId":
            return uuid.UUID(str(arguments.get(argument_key))) == uuid.UUID(match.group(binding))
        if binding is not None:
            return arguments.get(argument_key) == match.group(binding)
        return True
    except (GraphQLError, UnicodeDecodeError, ValueError, TypeError, KeyError, RecursionError):
        return False


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

        delegated = request.method == "POST" and request.path == "/graphql/"
        if delegated:
            if request.headers.get("Authorization"):
                return None
            method = request.headers.get("X-Op-Method", "").upper()
            path = request.headers.get("X-Op-Path", "")
            if not has_internal_secret(request) or not method or not path.startswith("/api/v1/admin/"):
                return None
        else:
            method = (request.method or "GET").upper()
            path = request.path.split("?")[0]

        # compute hash and try to find session
        try:
            h = key_hash(raw)
            sess = OpSession.objects.filter(key_hash=h).first()
        except Exception:
            return None

        if not sess or not sess.is_valid():
            return None

        # normalize operation and check allowed ops (reuse view helper logic if needed)
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

        if delegated and not _delegated_operation_allowed(request, method, path):
            return JsonResponse({"error": "Forbidden"}, status=403)

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
                    if delegated:
                        request.opkey_delegated = True
            except Exception:
                pass

        return None
