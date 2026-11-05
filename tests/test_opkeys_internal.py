import json
import os
import uuid
from contextlib import nullcontext
from datetime import timedelta
from io import StringIO
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import django
import jwt
import pytest
from django.test import RequestFactory, override_settings
from django.utils import timezone

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
django.setup()

from graphene_django.views import GraphQLView
from django.contrib.auth.models import AnonymousUser, User
from django.core.management import call_command
from django.db import connections
from opkeys import views
from opkeys.middleware import OpKeyAuthMiddleware, _delegated_operation_allowed
from saleor.graphql.api import AuthToken, LoginMutation, _authenticated_user, schema


INTERNAL_SECRET = "synthetic-internal-secret-0123456789-abcdef"


@pytest.mark.parametrize("method,path,query,allowed", [
    ("GET", "/api/v1/admin/audit",
     "query { adminAuditLogs { records { id } nextCursor } }", True),
    ("GET", "/api/v1/admin/audit",
     "query { adminCoupons { id } }", False),
    ("GET", "/api/v1/admin/coupons",
     "query { adminCoupons { id } }", True),
    ("GET", "/api/v1/admin/coupons",
     "query { adminAuditLogs { records { id } nextCursor } }", False),
    ("POST", "/api/v1/admin/coupons",
     'mutation { createAdminCoupon(input: {code: "SAVE10", discountPercent: 10, remaining: 1, active: true}) { userErrors { code } } }', True),
    ("POST", "/api/v1/admin/coupons",
     'mutation { createAdminCoupon(input: {code: "SAVE10", discountPercent: 10, remaining: 1, active: true}) { userErrors { code } } __typename }', False),
    ("GET", "/api/v1/admin/coupons/extra",
     "query { adminCoupons { id } }", False),
    ("PUT", "/api/v1/admin/coupons",
     'mutation { createAdminCoupon(input: {code: "SAVE10", discountPercent: 10, remaining: 1, active: true}) { userErrors { code } } }', False),
])
def test_audit_and_coupon_delegation_allow_only_exact_routes_and_roots(method, path, query, allowed):
    request = RequestFactory().post(
        "/graphql/",
        data=json.dumps({"query": query}),
        content_type="application/json",
    )
    assert _delegated_operation_allowed(request, method, path) is allowed


@pytest.mark.parametrize("method,path,query,allowed", [
    ("GET", "/api/v1/admin/sales-metrics", "query { adminSalesMetrics { __typename } }", True),
    ("GET", "/api/v1/admin/sales-metrics/categories", "query { adminSalesCategories }", True),
    ("GET", "/api/v1/admin/sales-metrics", "query { adminSalesCategories }", False),
    ("GET", "/api/v1/admin/sales-metrics/categories", "query { adminSalesMetrics { __typename } }", False),
    ("GET", "/api/v1/admin/sales-metrics", "query { adminCoupons { id } }", False),
    ("GET", "/api/v1/admin/sales-metrics/categories", "query { adminCoupons { id } }", False),
    ("POST", "/api/v1/admin/sales-metrics", "query { adminSalesMetrics { __typename } }", False),
    ("GET", "/api/v1/admin/sales-metrics/extra", "query { adminSalesMetrics { __typename } }", False),
    ("GET", "/api/v1/admin/sales-metrics", "query { adminSalesMetrics { __typename } adminSalesCategories }", False),
    ("GET", "/api/v1/admin/sales-metrics/categories", "query { adminSalesCategories adminSalesMetrics { __typename } }", False),
])
def test_sales_metrics_delegation_allows_only_exact_routes_and_roots(method, path, query, allowed):
    request = RequestFactory().post(
        "/graphql/",
        data=json.dumps({"query": query}),
        content_type="application/json",
    )
    assert _delegated_operation_allowed(request, method, path) is allowed


@pytest.mark.parametrize("argument,extra,allowed", [
    ("12345678-1234-4234-8234-123456789abc", "", True),
    ("12345678-1234-4234-8234-123456789abd", "", False),
    ("12345678-1234-4234-8234-123456789abc", " me { id }", False),
    ("invalid", "", False),
])
def test_stock_status_delegation_binds_movement_id(argument, extra, allowed):
    query = 'query ($id: ID!) { adminStockMovementStatus(movementId: $id) { movementId status }' + extra + ' }'
    request = RequestFactory().post(
        "/graphql/", data=json.dumps({"query": query, "variables": {"id": argument}}),
        content_type="application/json",
    )
    path = "/api/v1/admin/stock-movements/12345678-1234-4234-8234-123456789abc"
    assert _delegated_operation_allowed(request, "GET", path) is allowed


@pytest.mark.parametrize("product_id,extra,allowed", [
    ("42", "", True),
    ("43", "", False),
    ("42", " me { id }", False),
    ("42", ' other: adjustProductStock(productId: "42", movementId: "12345678-1234-4234-8234-123456789abc", direction: IN, quantity: 1) { status }', False),
    ("42", ' legacy: addProductStock(productId: "42", newStock: 1) { success }', False),
])
def test_stock_movement_delegation_binds_product(product_id, extra, allowed):
    query = 'mutation ($product: ID!) { adjustProductStock(productId: $product, movementId: "12345678-1234-4234-8234-123456789abc", direction: IN, quantity: 1) { status }' + extra + ' }'
    request = RequestFactory().post(
        "/graphql/", data=json.dumps({"query": query, "variables": {"product": product_id}}),
        content_type="application/json",
    )
    assert _delegated_operation_allowed(request, "POST", "/api/v1/admin/products/42/stock") is allowed


@pytest.mark.parametrize("argument,allowed", [
    ("12345678-1234-4234-8234-123456789abc", True),
    ("12345678-1234-4234-8234-123456789abd", False),
])
@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_stock_status_opkey_checks_path_before_resolver(argument, allowed):
    path = "/api/v1/admin/stock-movements/12345678-1234-4234-8234-123456789abc"
    query = 'query ($id: ID!) { adminStockMovementStatus(movementId: $id) { movementId status } }'
    request = RequestFactory().post(
        "/graphql/", data=json.dumps({"query": query, "variables": {"id": argument}}),
        content_type="application/json", HTTP_X_OP_KEY="synthetic-key", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
        HTTP_X_OP_METHOD="GET", HTTP_X_OP_PATH=path,
    )
    request.user = AnonymousUser()
    session = SimpleNamespace(user_id=1, ops=[f"GET:{path}"], is_valid=lambda: True, touch=Mock())
    field = schema.graphql_schema.query_type.fields["adminStockMovementStatus"]
    with patch("opkeys.middleware.OpSession.objects.filter") as sessions, patch("opkeys.middleware.get_user_model") as users, \
            patch.object(field, "resolve", Mock(return_value={"movement_id": argument, "status": "APPLIED"})) as resolver:
        sessions.return_value.first.return_value = session
        users.return_value.objects.filter.return_value.first.return_value = SimpleNamespace(pk=1, is_active=True, is_staff=True)
        response = OpKeyAuthMiddleware(GraphQLView.as_view(schema=schema))(request)
    assert response.status_code == (200 if allowed else 403)
    assert resolver.call_count == (1 if allowed else 0)
    if not allowed:
        session.touch.assert_not_called()


@pytest.mark.parametrize("view", [views.op_create, views.op_revoke, views.op_validate])
@pytest.mark.parametrize("header", [None, "incorrect-secret"])
@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_internal_operations_reject_unauthorized_requests_before_database_access(view, header):
    headers = {"HTTP_X_INTERNAL_SECRET": header} if header is not None else {}
    request = RequestFactory().post("/internal/test", data="{}", content_type="application/json", **headers)
    with patch.object(views.OpSession.objects, "create") as create, patch.object(
        views.OpSession.objects, "filter"
    ) as filter_sessions, patch.object(views.OpSession.objects, "get") as get:
        response = view(request)
    assert response.status_code == 403
    create.assert_not_called()
    filter_sessions.assert_not_called()
    get.assert_not_called()


@pytest.mark.parametrize("configured", ["", "changeme-internal-secret"])
@pytest.mark.parametrize("view", [views.op_create, views.op_revoke, views.op_validate])
def test_internal_operations_reject_missing_or_known_default_secret(view, configured):
    request = RequestFactory().post(
        "/internal/test", data="{}", content_type="application/json", HTTP_X_INTERNAL_SECRET=configured
    )
    with override_settings(OPKEY_INTERNAL_SECRET=configured), patch.object(
        views.OpSession.objects, "create", return_value=SimpleNamespace(id="synthetic-session", expires_at=timezone.now())
    ) as create, patch.object(views.OpSession.objects, "filter") as filter_sessions, patch.object(
        views.OpSession.objects, "get"
    ) as get:
        assert view(request).status_code == 403
    create.assert_not_called()
    filter_sessions.assert_not_called()
    get.assert_not_called()


@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_authorized_create_and_revoke_keep_existing_behavior():
    factory = RequestFactory()
    created = SimpleNamespace(id="synthetic-session", user_id=1, expires_at=timezone.now())
    with patch.object(views.OpSession.objects, "create", return_value=created) as create, patch(
        "opkeys.views.transaction.atomic", return_value=nullcontext()
    ), patch.object(views.OpKeyAudit.objects, "create"):
        response = views.op_create(factory.post(
            "/internal/op-create", data=json.dumps({"ops": ["GET:/api/test"], "user_id": 1}),
            content_type="application/json", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
        ))
    assert response.status_code == 200
    assert json.loads(response.content)["raw_key"]
    assert "no-store" in response.headers.get("Cache-Control", "")
    create.assert_called_once()

    session = SimpleNamespace(id=uuid.uuid4(), user_id=1, revoked=False, save=Mock())
    with patch.object(views.OpSession.objects, "filter") as filter_sessions, patch(
        "opkeys.views.transaction.atomic", return_value=nullcontext()
    ), patch.object(views.OpKeyAudit.objects, "create"):
        filter_sessions.return_value.select_for_update.return_value = [session]
        response = views.op_revoke(factory.post(
            "/internal/op-revoke", data=json.dumps({"session_id": "synthetic-session"}),
            content_type="application/json", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
        ))
    assert response.status_code == 200
    assert json.loads(response.content)["revoked"] == 1


@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_revocation_audits_reason_without_logging_operation_key(caplog):
    raw_key = "synthetic-private-operation-key"
    request = RequestFactory().post(
        "/internal/op-revoke", data=json.dumps({"op_key": raw_key, "reason": "logout"}),
        content_type="application/json", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
    )
    caplog.set_level("INFO", logger="opkeys.views")
    session = SimpleNamespace(id=uuid.uuid4(), user_id=1, revoked=False, save=Mock())
    with patch.object(views.OpSession.objects, "filter") as sessions, patch(
        "opkeys.views.transaction.atomic", return_value=nullcontext()
    ), patch.object(views.OpKeyAudit.objects, "create"):
        sessions.return_value.select_for_update.return_value = [session]
        response = views.op_revoke(request)
    assert response.status_code == 200
    assert "logout" in caplog.text
    assert raw_key not in caplog.text


@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_expired_validation_audits_reason_without_logging_operation_key(caplog):
    raw_key = "synthetic-private-operation-key"
    session = SimpleNamespace(id="synthetic-session", pk="synthetic-session", user_id=1,
                              revoked=False, is_valid=lambda: False, is_expired=lambda: True)
    request = RequestFactory().post(
        "/internal/op-validate", data=json.dumps({"op_key": raw_key, "path": "/api/v1/admin/products"}),
        content_type="application/json", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
    )
    caplog.set_level("INFO", logger="opkeys.views")
    with patch.object(views.OpSession.objects, "get", return_value=session), patch.object(
        views.OpSession.objects, "select_for_update"
    ) as locked, patch("opkeys.views.transaction.atomic", return_value=nullcontext()), patch.object(
        views.OpKeyAudit.objects, "filter"
    ) as audits, patch.object(views.OpKeyAudit.objects, "create"):
        locked.return_value.get.return_value = session
        audits.return_value.exists.return_value = False
        response = views.op_validate(request)
    assert json.loads(response.content)["valid"] is False
    assert "expiry" in caplog.text
    assert raw_key not in caplog.text


@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_authorized_validation_keeps_existing_behavior():
    session = SimpleNamespace(
        id="synthetic-session", user_id=1, ops=["GET:/api/test"], expires_at=timezone.now(),
        is_valid=lambda: True, touch=lambda: None,
    )
    request = RequestFactory().post(
        "/internal/op-validate", data=json.dumps({"op_key": "synthetic-key", "path": "/api/test"}),
        content_type="application/json", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
    )
    with patch.object(views.OpSession.objects, "get", return_value=session):
        response = views.op_validate(request)
    assert response.status_code == 200
    assert json.loads(response.content)["valid"] is True


@pytest.mark.parametrize("active,staff,expected", [(True, True, True), (False, True, False), (True, False, False)])
@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_admin_operation_requires_active_administrator(active, staff, expected):
    session = SimpleNamespace(
        id="synthetic-session", user_id=1, user=SimpleNamespace(is_active=active, is_staff=staff, is_superuser=False),
        ops=["GET:/api/v1/admin/products"], expires_at=timezone.now(), is_valid=lambda: True, touch=lambda: None,
    )
    request = RequestFactory().post(
        "/internal/op-validate", data=json.dumps({"op_key": "synthetic-key", "path": "/api/v1/admin/products"}),
        content_type="application/json", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
    )
    with patch.object(views.OpSession.objects, "get", return_value=session):
        response = views.op_validate(request)
    assert json.loads(response.content)["valid"] is expected


@pytest.mark.parametrize("internal_secret,path,expected", [
    (INTERNAL_SECRET, "/api/v1/admin/products", True),
    ("incorrect-secret", "/api/v1/admin/products", False),
    (INTERNAL_SECRET, "/api/v1/admin/other", False),
    (INTERNAL_SECRET, "/api/v1/products", False),
])
@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_graphql_delegation_requires_trusted_secret_and_allowed_operation(internal_secret, path, expected):
    request = RequestFactory().post(
        "/graphql/", data=json.dumps({"query": 'mutation { createProduct(input: {name: "Game", price: 1, quantity: 1}) { __typename } }'}), content_type="application/json",
        HTTP_X_OP_KEY="synthetic-operation-key", HTTP_X_INTERNAL_SECRET=internal_secret,
        HTTP_X_OP_METHOD="POST", HTTP_X_OP_PATH=path,
    )
    request.user = AnonymousUser()
    user = SimpleNamespace(pk=1, is_active=True, is_staff=True, is_superuser=False)
    session = SimpleNamespace(user_id=1, ops=["POST:/api/v1/admin/products"],
                              is_valid=lambda: True, touch=lambda: None)
    with patch("opkeys.middleware.OpSession.objects.filter") as sessions, patch(
        "opkeys.middleware.get_user_model"
    ) as users:
        sessions.return_value.first.return_value = session
        users.return_value.objects.filter.return_value.first.return_value = user
        OpKeyAuthMiddleware(lambda value: None).process_request(request)
    assert (_authenticated_user(SimpleNamespace(context=request)) is user) is expected


@pytest.mark.parametrize("method,path,document,variables,allowed", [
    ("GET", "/api/v1/admin/op-sessions", "query { adminOpSessions { hasNext } }", {}, True),
    ("POST", "/api/v1/admin/op-sessions/11111111-2222-4333-8444-555555555555/revoke", 'mutation($id: ID!, $action: String!) { manageOpSession(sessionId: $id, action: $action) { success } }', {"id": "11111111-2222-4333-8444-555555555555", "action": "REVOKE"}, True),
    ("POST", "/api/v1/admin/op-sessions/11111111-2222-4333-8444-555555555555/expire", 'mutation { manageOpSession(sessionId: "11111111-2222-4333-8444-555555555555", action: "EXPIRE") { success } }', {}, True),
    ("POST", "/api/v1/admin/op-sessions/11111111-2222-4333-8444-555555555555/revoke", 'mutation { manageOpSession(sessionId: "11111111-2222-4333-8444-555555555555", action: "EXPIRE") { success } }', {}, False),
    ("POST", "/api/v1/admin/op-sessions/11111111-2222-4333-8444-555555555555/revoke", 'mutation { manageOpSession(sessionId: "11111111-2222-4333-8444-555555555556", action: "REVOKE") { success } }', {}, False),
    ("POST", "/api/v1/admin/gift-cards", 'mutation { createGiftCard(amount: "10") { __typename } }', {}, True),
    ("POST", "/api/v1/admin/products", 'mutation { createProduct(input: {name: "Game", price: 1, quantity: 1}) { __typename } }', {}, True),
    ("PUT", "/api/v1/admin/products/42", 'mutation($id: ID!) { updateProduct(id: $id, input: {name: "Game"}) { __typename } }', {"id": "42"}, True),
    ("PUT", "/api/v1/admin/products/42", 'mutation { updateProduct(id: "43", input: {name: "Game"}) { __typename } }', {}, False),
    ("POST", "/api/v1/admin/products/42/stock", 'mutation { addProductStock(productId: "43", newStock: 1) { success } }', {}, False),
    ("POST", "/api/v1/admin/products/00042/stock", 'mutation { addProductStock(productId: "42", newStock: 1) { success } }', {}, True),
    ("GET", "/api/v1/admin/promotions", 'query { promotions { id } }', {}, True),
    ("GET", "/api/v1/admin/promotions", 'query ListPromotions { promotions { id name discountPercent active } }', {}, False),
    ("POST", "/api/v1/admin/promotions", 'mutation { createPromotion(input: {name: "Sale", discountPercent: 1, active: true}) { __typename } }', {}, True),
    ("PUT", "/api/v1/admin/promotions/42", 'mutation { updatePromotion(id: 42, input: {name: "Sale"}) { __typename } }', {}, True),
    ("PUT", "/api/v1/admin/promotions/42", 'mutation { updatePromotion(id: 43, input: {name: "Sale"}) { __typename } }', {}, False),
    ("POST", "/api/v1/admin/products", 'mutation { createPromotion(input: {name: "Sale", discountPercent: 1, active: true}) { __typename } }', {}, False),
    ("POST", "/api/v1/admin/unknown", 'mutation { createProduct(input: {name: "Game", price: 1, quantity: 1}) { __typename } }', {}, False),
    ("GET", "/api/v1/admin/products/42", 'query { productByBggId(bggId: 42) { name } }', {}, True),
    ("GET", "/api/v1/admin/products/42", 'query($id: Int!) { productByBggId(bggId: $id) { name } }', {"id": 42}, True),
    ("GET", "/api/v1/admin/products/42", 'query { productByBggId(bggId: 43) { name } }', {}, False),
    ("GET", "/api/v1/admin/products/42x", 'query { productByBggId(bggId: 42) { name } }', {}, False),
    ("GET", "/api/v1/admin/products/42", 'query { adminCoupons { id } }', {}, False),
    ("POST", "/api/v1/admin/products/42", 'query { productByBggId(bggId: 42) { name } }', {}, False),
    ("PATCH", "/api/v1/admin/coupons/7", 'mutation { updateAdminCoupon(id: 7, input: {active: false}) { userErrors { code } } }', {}, True),
    ("PATCH", "/api/v1/admin/coupons/7", 'mutation($id: Int!) { updateAdminCoupon(id: $id, input: {active: false}) { userErrors { code } } }', {"id": 7}, True),
    ("PATCH", "/api/v1/admin/coupons/7", 'mutation { updateAdminCoupon(id: 8, input: {active: false}) { userErrors { code } } }', {}, False),
    ("PATCH", "/api/v1/admin/coupons/abc", 'mutation { updateAdminCoupon(id: 7, input: {active: false}) { userErrors { code } } }', {}, False),
    ("PATCH", "/api/v1/admin/coupons/7", 'mutation { createAdminCoupon(input: {code: "A", discountPercent: 1, remaining: 1, active: true}) { userErrors { code } } }', {}, False),
    ("PUT", "/api/v1/admin/coupons/7", 'mutation { updateAdminCoupon(id: 7, input: {active: false}) { userErrors { code } } }', {}, False),
    ("POST", "/api/v1/admin/coupons/CODE/apply", 'mutation { applyCouponPreview(code: "CODE") { valid } }', {}, False),
])
@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_delegation_binds_route_operation_and_arguments(method, path, document, variables, allowed):
    request = RequestFactory().post(
        "/graphql/", data=json.dumps({"query": document, "variables": variables}), content_type="application/json",
        HTTP_X_OP_KEY="synthetic-key", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
        HTTP_X_OP_METHOD=method, HTTP_X_OP_PATH=path,
    )
    request.user = AnonymousUser()
    user = SimpleNamespace(pk=1, is_active=True, is_staff=True, is_superuser=False)
    session = SimpleNamespace(user_id=1, ops=[f"{method}:{path}"], is_valid=lambda: True, touch=Mock())
    with patch("opkeys.middleware.OpSession.objects.filter") as sessions, patch("opkeys.middleware.get_user_model") as users:
        sessions.return_value.first.return_value = session
        users.return_value.objects.filter.return_value.first.return_value = user
        response = OpKeyAuthMiddleware(lambda value: None).process_request(request)
    assert (response is None and getattr(request, "opkey_delegated", False)) is allowed
    if not allowed:
        assert response.status_code == 403
        session.touch.assert_not_called()


@pytest.mark.parametrize("method,path,document,field_name,result", [
    ("GET", "/api/v1/admin/op-sessions", "{ adminOpSessions { hasNext } }", "adminOpSessions", {"has_next": False}),
    ("POST", "/api/v1/admin/op-sessions/11111111-2222-4333-8444-555555555555/revoke", 'mutation { manageOpSession(sessionId: "11111111-2222-4333-8444-555555555555", action: "REVOKE") { success } }', "manageOpSession", {"success": True}),
    ("POST", "/api/v1/admin/op-sessions/11111111-2222-4333-8444-555555555555/expire", 'mutation { manageOpSession(sessionId: "11111111-2222-4333-8444-555555555555", action: "EXPIRE") { success } }', "manageOpSession", {"success": True}),
    ("POST", "/api/v1/admin/gift-cards", 'mutation { createGiftCard(amount: "10") { __typename } }', "createGiftCard", {}),
    ("POST", "/api/v1/admin/products", 'mutation { createProduct(input: {name: "Game", price: 1, quantity: 1}) { __typename } }', "createProduct", {}),
    ("PUT", "/api/v1/admin/products/42", 'mutation { updateProduct(id: "42", input: {name: "Game"}) { __typename } }', "updateProduct", {}),
    ("POST", "/api/v1/admin/products/42/stock", 'mutation { addProductStock(productId: "42", newStock: 1) { success } }', "addProductStock", {"success": True}),
    ("GET", "/api/v1/admin/promotions", "{ promotions { id } }", "promotions", []),
    ("POST", "/api/v1/admin/promotions", 'mutation { createPromotion(input: {name: "Sale", discountPercent: 1, active: true}) { __typename } }', "createPromotion", {}),
    ("PUT", "/api/v1/admin/promotions/42", 'mutation { updatePromotion(id: "42", input: {name: "Sale"}) { __typename } }', "updatePromotion", {}),
])
@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_each_observed_delegation_executes_its_permitted_resolver(method, path, document, field_name, result):
    request = RequestFactory().post(
        "/graphql/", data=json.dumps({"query": document}), content_type="application/json",
        HTTP_X_OP_KEY="synthetic-key", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
        HTTP_X_OP_METHOD=method, HTTP_X_OP_PATH=path,
    )
    request.user = AnonymousUser()
    session = SimpleNamespace(user_id=1, ops=[f"{method}:{path}"], is_valid=lambda: True, touch=Mock())
    user = SimpleNamespace(pk=1, is_active=True, is_staff=True, is_superuser=False)
    root = schema.graphql_schema.query_type if method == "GET" else schema.graphql_schema.mutation_type
    with patch("opkeys.middleware.OpSession.objects.filter") as sessions, patch("opkeys.middleware.get_user_model") as users, \
            patch.object(root.fields[field_name], "resolve", Mock(return_value=result)) as resolver:
        sessions.return_value.first.return_value = session
        users.return_value.objects.filter.return_value.first.return_value = user
        response = OpKeyAuthMiddleware(GraphQLView.as_view(schema=schema))(request)
    assert response.status_code == 200
    assert "errors" not in json.loads(response.content)
    resolver.assert_called_once()


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/v1/admin/op-sessions"),
    ("POST", "/api/v1/admin/op-sessions/11111111-2222-4333-8444-555555555555/revoke"),
    ("POST", "/api/v1/admin/op-sessions/11111111-2222-4333-8444-555555555555/expire"),
    ("POST", "/api/v1/admin/gift-cards"),
    ("POST", "/api/v1/admin/products"),
    ("PUT", "/api/v1/admin/products/42"),
    ("POST", "/api/v1/admin/products/42/stock"),
    ("GET", "/api/v1/admin/promotions"),
    ("POST", "/api/v1/admin/promotions"),
    ("PUT", "/api/v1/admin/promotions/42"),
])
@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_each_observed_delegation_rejects_another_root_before_execution(method, path):
    document = ('{ __typename }' if method == "GET" else
                'mutation { createProduct(input: {name: "Game", price: 1, quantity: 1}) { __typename } }' if path == "/api/v1/admin/promotions" else
                'mutation { createPromotion(input: {name: "Sale", discountPercent: 1, active: true}) { __typename } }')
    request = RequestFactory().post(
        "/graphql/", data=json.dumps({"query": document}), content_type="application/json",
        HTTP_X_OP_KEY="synthetic-key", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
        HTTP_X_OP_METHOD=method, HTTP_X_OP_PATH=path,
    )
    request.user = AnonymousUser()
    session = SimpleNamespace(user_id=1, ops=[f"{method}:{path}"], is_valid=lambda: True, touch=Mock())
    with patch("opkeys.middleware.OpSession.objects.filter") as sessions:
        sessions.return_value.first.return_value = session
        response = OpKeyAuthMiddleware(Mock())(request)
    assert response.status_code == 403
    session.touch.assert_not_called()


@pytest.mark.parametrize("second_id", ["42", "43"])
@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_stock_aliases_rejected_before_any_resolver(second_id):
    document = 'mutation { first: addProductStock(productId: "42", newStock: 1) { success } second: addProductStock(productId: "%s", newStock: 1) { success } }' % second_id
    request = RequestFactory().post(
        "/graphql/", data=json.dumps({"query": document}), content_type="application/json",
        HTTP_X_OP_KEY="synthetic-key", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
        HTTP_X_OP_METHOD="POST", HTTP_X_OP_PATH="/api/v1/admin/products/42/stock",
    )
    request.user = AnonymousUser()
    session = SimpleNamespace(user_id=1, ops=["POST:/api/v1/admin/products/42/stock"], is_valid=lambda: True, touch=Mock())
    field = schema.graphql_schema.mutation_type.fields["addProductStock"]
    with patch("opkeys.middleware.OpSession.objects.filter") as sessions, patch("opkeys.middleware.get_user_model") as users, \
            patch.object(field, "resolve", Mock(return_value={"success": True})) as resolver:
        sessions.return_value.first.return_value = session
        users.return_value.objects.filter.return_value.first.return_value = SimpleNamespace(pk=1, is_active=True, is_staff=True)
        response = OpKeyAuthMiddleware(GraphQLView.as_view(schema=schema))(request)
    assert response.status_code == 403
    resolver.assert_not_called()
    session.touch.assert_not_called()


@pytest.mark.parametrize("document,operation,variables,allowed", [
    ('mutation Stock($id: ID! = "42") { addProductStock(productId: $id, newStock: 1) { success } }', "Stock", {}, True),
    ('mutation Stock($id: ID!) { addProductStock(productId: $id, newStock: 1) { success } }', "Stock", {"id": []}, False),
    ('mutation Stock($id: ID!) { addProductStock(productId: $id, newStock: 1) { success } }', "Stock", {}, False),
    ('mutation Stock { ...First ...Second } fragment First on Mutation { one: addProductStock(productId: "42", newStock: 1) { success } } fragment Second on Mutation { two: addProductStock(productId: "42", newStock: 1) { success } }', "Stock", {}, False),
    ('mutation Stock { one: addProductStock(productId: "42", newStock: 1) { success } two: addProductStock(productId: "42", newStock: 1) @skip(if: true) { success } }', "Stock", {}, False),
    ('mutation Stock { addProductStock(productId: "42", newStock: 1) { success } } mutation Other { __typename }', None, {}, False),
])
@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_stock_delegation_coerces_variables_and_rejects_ambiguous_roots(document, operation, variables, allowed):
    request = RequestFactory().post(
        "/graphql/", data=json.dumps({"query": document, "operationName": operation, "variables": variables}),
        content_type="application/json", HTTP_X_OP_KEY="synthetic-key", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
        HTTP_X_OP_METHOD="POST", HTTP_X_OP_PATH="/api/v1/admin/products/42/stock",
    )
    request.user = AnonymousUser()
    session = SimpleNamespace(user_id=1, ops=["POST:/api/v1/admin/products/42/stock"], is_valid=lambda: True, touch=Mock())
    user = SimpleNamespace(pk=1, is_active=True, is_staff=True, is_superuser=False)
    with patch("opkeys.middleware.OpSession.objects.filter") as sessions, patch("opkeys.middleware.get_user_model") as users:
        sessions.return_value.first.return_value = session
        users.return_value.objects.filter.return_value.first.return_value = user
        response = OpKeyAuthMiddleware(lambda value: None).process_request(request)
    assert (response is None and getattr(request, "opkey_delegated", False)) is allowed
    if not allowed:
        assert response.status_code == 403
        session.touch.assert_not_called()


@pytest.mark.parametrize("valid_jwt", [True, False])
@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET, SECRET_KEY="synthetic-jwt-secret-long-enough-for-tests")
def test_whoami_with_jwt_and_opkey_uses_only_jwt_identity(valid_jwt):
    from django.conf import settings

    jwt_user = SimpleNamespace(pk=7, email="jwt@example.test", first_name="", last_name="", is_active=True, is_staff=True, is_superuser=False)
    token = jwt.encode({"sub": "7", "token_type": "access"}, settings.SECRET_KEY, algorithm="HS256") if valid_jwt else "invalid"
    request = RequestFactory().post(
        "/graphql/", data=json.dumps({"query": "query WhoAmI { me { id email isStaff isSuperuser } }"}),
        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_OP_KEY="synthetic-key",
        HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET, HTTP_X_OP_METHOD="POST", HTTP_X_OP_PATH="/api/v1/admin/products/42/stock",
    )
    request.user = AnonymousUser()
    with patch("opkeys.middleware.OpSession.objects.filter") as sessions, patch(
        "django.contrib.auth.models.User.objects.get", return_value=jwt_user
    ):
        response = OpKeyAuthMiddleware(GraphQLView.as_view(schema=schema))(request)
    assert response.status_code == 200
    identity = json.loads(response.content)["data"]["me"]
    assert identity == ({"id": "7", "email": "jwt@example.test", "isStaff": True, "isSuperuser": False} if valid_jwt else None)
    assert not getattr(request, "opkey_delegated", False)
    sessions.assert_not_called()


@pytest.mark.parametrize("document,operation,variables,allowed", [
    ("mutation { addProductStock(productId: \"42\", newStock: 1) { success } }", None, {}, True),
    ("mutation Stock($id: ID!, $amount: Int!, $skip: Boolean!) { alias: addProductStock(productId: $id, newStock: $amount) @skip(if: $skip) { success } }", "Stock", {"id": "42", "amount": 1, "skip": False}, True),
    ("mutation { ...StockFields } fragment StockFields on Mutation { addProductStock(productId: \"42\", newStock: 1) { success } }", None, {}, True),
    ("mutation { ... on Mutation { addProductStock(productId: \"42\", newStock: 1) { success } } }", None, {}, True),
    ("mutation Safe { addProductStock(productId: \"42\", newStock: 1) { success } } mutation Other { __typename }", "Safe", {}, True),
    ("mutation Safe { addProductStock(productId: \"42\", newStock: 1) { success } } mutation Other { __typename }", "Other", {}, False),
    ("mutation Safe { addProductStock(productId: \"42\", newStock: 1) { success } }", "Missing", {}, False),
    ("mutation { __typename addProductStock(productId: \"42\", newStock: 1) { success } }", None, {}, False),
    ("mutation { addProductStock(productId: \"42\", newStock: 1) { success } __typename }", None, {}, False),
    ("mutation { ...Other } fragment Other on Mutation { __typename }", None, {}, False),
    ("mutation { __typename @skip(if: true) }", None, {}, False),
    ("query { __typename }", None, {}, False),
    ("mutation Safe { __typename } mutation Other { __typename }", None, {}, False),
    ("mutation { ...Missing }", None, {}, False),
    ("mutation { ...Cycle } fragment Cycle on Mutation { ...Cycle }", None, {}, False),
    ("mutation {", None, {}, False),
])
@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_stock_delegation_filters_entire_selected_graphql_operation(document, operation, variables, allowed):
    request = RequestFactory().post(
        "/graphql/", data=json.dumps({"query": document, "operationName": operation, "variables": variables}),
        content_type="application/json", HTTP_X_OP_KEY="synthetic-key", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
        HTTP_X_OP_METHOD="POST", HTTP_X_OP_PATH="/api/v1/admin/products/42/stock",
    )
    request.user = AnonymousUser()
    user = SimpleNamespace(pk=1, is_active=True, is_staff=True, is_superuser=False)
    session = SimpleNamespace(user_id=1, ops=["POST:/api/v1/admin/products/42/stock"],
                              is_valid=lambda: True, touch=Mock())
    with patch("opkeys.middleware.OpSession.objects.filter") as sessions, patch("opkeys.middleware.get_user_model") as users:
        sessions.return_value.first.return_value = session
        users.return_value.objects.filter.return_value.first.return_value = user
        response = OpKeyAuthMiddleware(lambda value: None).process_request(request)
    assert (response is None) is allowed
    if not allowed:
        assert response.status_code in (400, 403)


@pytest.mark.parametrize("body,content_type,suffix", [
    ('[{"query":"mutation { __typename }"}]', "application/json", ""),
    ("mutation { __typename }", "application/graphql", ""),
    ("query=mutation%20%7B%20__typename%20%7D", "application/x-www-form-urlencoded", ""),
    ('{"query":', "application/json", ""),
    ('{"query":"mutation { __typename }", "variables":"{}"}', "application/json", ""),
    ('{"query":"mutation { __typename }", "operationName":1}', "application/json", ""),
    (json.dumps({"query": 'mutation { addProductStock(productId: "42", newStock: 1) { success } }'}), "application/json", "?query=mutation%20%7B%20__typename%20%7D"),
])
@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_stock_delegation_rejects_unsupported_transports(body, content_type, suffix):
    request = RequestFactory().post(
        "/graphql/" + suffix, data=body, content_type=content_type,
        HTTP_X_OP_KEY="synthetic-key", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
        HTTP_X_OP_METHOD="POST", HTTP_X_OP_PATH="/api/v1/admin/products/42/stock",
    )
    request.user = AnonymousUser()
    session = SimpleNamespace(user_id=1, ops=["POST:/api/v1/admin/products/42/stock"],
                              is_valid=lambda: True, touch=Mock())
    with patch("opkeys.middleware.OpSession.objects.filter") as sessions:
        sessions.return_value.first.return_value = session
        response = OpKeyAuthMiddleware(lambda value: None).process_request(request)
    assert response.status_code == 403
    session.touch.assert_not_called()


@pytest.mark.parametrize("internal_secret,valid,ops", [
    ("incorrect-secret", True, ["POST:/api/v1/admin/products/42/stock"]),
    (INTERNAL_SECRET, False, ["POST:/api/v1/admin/products/42/stock"]),
    (INTERNAL_SECRET, True, ["POST:/api/v1/admin/products/other"]),
])
@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_stock_delegation_does_not_trust_unvalidated_headers(internal_secret, valid, ops):
    request = RequestFactory().post(
        "/graphql/", data=json.dumps({"query": "mutation { __typename }"}), content_type="application/json",
        HTTP_X_OP_KEY="synthetic-key", HTTP_X_INTERNAL_SECRET=internal_secret,
        HTTP_X_OP_METHOD="POST", HTTP_X_OP_PATH="/api/v1/admin/products/42/stock",
    )
    request.user = AnonymousUser()
    session = SimpleNamespace(user_id=1, ops=ops, is_valid=lambda: valid, touch=Mock())
    with patch("opkeys.middleware.OpSession.objects.filter") as sessions:
        sessions.return_value.first.return_value = session
        assert OpKeyAuthMiddleware(lambda value: None).process_request(request) is None
    assert not getattr(request, "opkey_delegated", False)
    assert request.user.is_anonymous
    session.touch.assert_not_called()


@pytest.mark.parametrize("stock_first,fragment", [(True, False), (False, False), (True, True)])
@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_stock_delegation_rejects_mixed_mutations_before_any_resolver(stock_first, fragment):
    stock = 'addProductStock(productId: "42", newStock: 1) { success }'
    promotion = 'createPromotion(input: {name: "test", discountPercent: 1, active: true}) { __typename }'
    if fragment:
        promotion = "...Other"
    selections = [stock, promotion] if stock_first else [promotion, stock]
    document = "mutation { " + " ".join(selections) + " }"
    if fragment:
        document += ' fragment Other on Mutation { createPromotion(input: {name: "test", discountPercent: 1, active: true}) { __typename } }'
    request = RequestFactory().post(
        "/graphql/", data=json.dumps({"query": document}),
        content_type="application/json", HTTP_X_OP_KEY="synthetic-key", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
        HTTP_X_OP_METHOD="POST", HTTP_X_OP_PATH="/api/v1/admin/products/42/stock",
    )
    request.user = AnonymousUser()
    user = SimpleNamespace(pk=1, is_active=True, is_staff=True, is_superuser=False)
    session = SimpleNamespace(user_id=1, ops=["POST:/api/v1/admin/products/42/stock"],
                              is_valid=lambda: True, touch=Mock())
    graphql_view = GraphQLView.as_view(schema=schema)
    stock_field = schema.graphql_schema.mutation_type.fields["addProductStock"]
    promotion_field = schema.graphql_schema.mutation_type.fields["createPromotion"]
    with patch("opkeys.middleware.OpSession.objects.filter") as sessions, patch("opkeys.middleware.get_user_model") as users, \
            patch.object(stock_field, "resolve", Mock()) as stock_resolver, \
            patch.object(promotion_field, "resolve", Mock()) as promotion_resolver:
        sessions.return_value.first.return_value = session
        users.return_value.objects.filter.return_value.first.return_value = user
        response = OpKeyAuthMiddleware(graphql_view)(request)
    assert response.status_code == 403
    stock_resolver.assert_not_called()
    promotion_resolver.assert_not_called()


@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_stock_delegation_executes_selected_stock_resolver():
    request = RequestFactory().post(
        "/graphql/", data=json.dumps({
            "query": 'mutation Stock($id: ID!, $amount: Int!) { alias: addProductStock(productId: $id, newStock: $amount) { success } }',
            "operationName": "Stock", "variables": {"id": "42", "amount": 1},
        }), content_type="application/json", HTTP_X_OP_KEY="synthetic-key", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
        HTTP_X_OP_METHOD="POST", HTTP_X_OP_PATH="/api/v1/admin/products/42/stock",
    )
    request.user = AnonymousUser()
    user = SimpleNamespace(pk=1, is_active=True, is_staff=True, is_superuser=False)
    session = SimpleNamespace(user_id=1, ops=["POST:/api/v1/admin/products/42/stock"],
                              is_valid=lambda: True, touch=Mock())
    field = schema.graphql_schema.mutation_type.fields["addProductStock"]
    with patch("opkeys.middleware.OpSession.objects.filter") as sessions, patch("opkeys.middleware.get_user_model") as users, \
            patch.object(field, "resolve", Mock(return_value={"success": True})) as resolver:
        sessions.return_value.first.return_value = session
        users.return_value.objects.filter.return_value.first.return_value = user
        response = OpKeyAuthMiddleware(GraphQLView.as_view(schema=schema))(request)
    assert response.status_code == 200
    assert json.loads(response.content)["data"] == {"alias": {"success": True}}
    resolver.assert_called_once()


@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_stock_delegation_does_not_restrict_bearer_requests():
    request = RequestFactory().post(
        "/graphql/", data=json.dumps({"query": "mutation { __typename }"}), content_type="application/json",
        HTTP_AUTHORIZATION="Bearer synthetic-jwt", HTTP_X_OP_KEY="synthetic-key", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
        HTTP_X_OP_METHOD="POST", HTTP_X_OP_PATH="/api/v1/admin/products/42/stock",
    )
    request.user = AnonymousUser()
    session = SimpleNamespace(user_id=1, ops=["POST:/api/v1/admin/products/42/stock"],
                              is_valid=lambda: True, touch=Mock())
    with patch("opkeys.middleware.OpSession.objects.filter") as sessions:
        sessions.return_value.first.return_value = session
        response = OpKeyAuthMiddleware(GraphQLView.as_view(schema=schema))(request)
    assert response.status_code == 200
    assert json.loads(response.content)["data"]["__typename"] == "Mutation"
    assert not getattr(request, "opkey_delegated", False)


@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_graphql_delegation_does_not_replace_invalid_bearer():
    request = RequestFactory().post("/graphql/", HTTP_AUTHORIZATION="Bearer invalid")
    request.user = SimpleNamespace(pk=1, is_active=True, is_staff=True)
    request.op_session = SimpleNamespace(user_id=1)
    assert _authenticated_user(SimpleNamespace(context=request)) is None


@override_settings(OPKEYS_ISSUE_ON_LOGIN=True, OPKEY_INTERNAL_SECRET="")
def test_login_keeps_jwt_without_issuing_unusable_operation_key():
    user = SimpleNamespace(pk=1, email="admin@example.test", first_name="", last_name="", is_active=True,
                           is_staff=True, is_superuser=False)
    credentials = SimpleNamespace(email="admin@example.test", password="synthetic-password", guest_cart_id=None)
    with patch("django.contrib.auth.authenticate", return_value=user), patch(
        "saleor.graphql.api._auth_tokens", return_value=AuthToken(token="synthetic-jwt")
    ), patch.object(views.OpSession.objects, "create") as create:
        result = LoginMutation.mutate(None, SimpleNamespace(context=None), credentials)
    assert result.tokens.token == "synthetic-jwt"
    assert result.tokens.op_key is None
    create.assert_not_called()


@pytest.mark.parametrize("staff", [True, False])
@override_settings(OPKEYS_ISSUE_ON_LOGIN=True, OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_login_issues_operation_key_only_for_administrators(staff):
    user = SimpleNamespace(pk=1, email="admin@example.test", first_name="", last_name="", is_active=True,
                           is_staff=staff, is_superuser=False)
    credentials = SimpleNamespace(email="admin@example.test", password="synthetic-password", guest_cart_id=None)
    session = SimpleNamespace(id=uuid.uuid4(), expires_at=timezone.now(), ops=["GET:/api/v1/admin/*"])
    with patch("django.contrib.auth.authenticate", return_value=user), patch(
        "saleor.graphql.api._auth_tokens", return_value=AuthToken(token="synthetic-jwt")
    ), patch("opkeys.utils.generate_raw_key", return_value="synthetic-op-key"), patch.object(
        views.OpSession.objects, "create", return_value=session
    ) as create, patch("saleor.graphql.api.transaction.atomic", return_value=nullcontext()), patch.object(
        views.OpKeyAudit.objects, "create"
    ):
        result = LoginMutation.mutate(None, SimpleNamespace(context=None), credentials)
    assert result.tokens.token == "synthetic-jwt"
    assert result.tokens.op_key == ("synthetic-op-key" if staff else None)
    assert create.call_count == (1 if staff else 0)


@override_settings(OPKEYS_ISSUE_ON_LOGIN=True, OPKEY_INTERNAL_SECRET=INTERNAL_SECRET, USE_TZ=False)
def test_login_operation_key_expiry_has_explicit_timezone():
    user = SimpleNamespace(pk=1, email="admin@example.test", first_name="", last_name="", is_active=True,
                           is_staff=True, is_superuser=False)
    credentials = SimpleNamespace(email="admin@example.test", password="synthetic-password", guest_cart_id=None)
    session = SimpleNamespace(id=uuid.uuid4(), expires_at=timezone.now(), ops=["GET:/api/v1/admin/*"])
    with patch("django.contrib.auth.authenticate", return_value=user), patch(
        "saleor.graphql.api._auth_tokens", return_value=AuthToken(token="synthetic-jwt")
    ), patch("opkeys.utils.generate_raw_key", return_value="synthetic-op-key"), patch.object(
        views.OpSession.objects, "create", return_value=session
    ), patch("saleor.graphql.api.transaction.atomic", return_value=nullcontext()), patch.object(
        views.OpKeyAudit.objects, "create"
    ):
        result = LoginMutation.mutate(None, SimpleNamespace(context=None), credentials)
    assert result.tokens.op_key_expires_at.endswith("+00:00")


@override_settings(OPKEY_INTERNAL_SECRET=INTERNAL_SECRET)
def test_operation_key_lifecycle_against_isolated_sqlite_database():
    alias = f"isolated_opkeys_{uuid.uuid4().hex}"
    connections.databases[alias] = {
        **connections.databases["default"], "ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:", "OPTIONS": {},
    }
    database = connections[alias]
    try:
        with database.schema_editor() as editor:
            editor.create_model(User)
            editor.create_model(views.OpSession)
            editor.create_model(views.OpKeyAudit)
        user = User.objects.db_manager(alias).create_user(
            username="synthetic-admin", password="synthetic-password", is_staff=True,
        )
        manager = views.OpSession.objects.db_manager(alias)
        audits = views.OpKeyAudit.objects.db_manager(alias)
        factory = RequestFactory()
        with patch.object(views.OpSession, "objects", manager), patch.object(views.OpKeyAudit, "objects", audits):
            created = views.op_create(factory.post(
                "/internal/op-create", data=json.dumps({"ops": ["GET:/api/v1/admin/products"], "user_id": user.pk}),
                content_type="application/json", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
            ))
            raw_key = json.loads(created.content)["raw_key"]
            with patch("saleor.graphql.api._authenticated_user", return_value=user):
                listed = schema.execute(
                    '{ adminOpSessions { items { id userId ops revoked } hasNext userErrors { code } } }'
                )
                assert not listed.errors
                assert len(listed.data["adminOpSessions"]["items"]) == 1
                assert raw_key not in str(listed.data)
                identifier = listed.data["adminOpSessions"]["items"][0]["id"]
                expired = schema.execute(
                    'mutation($id: ID!) { manageOpSession(sessionId: $id, action: "EXPIRE") '
                    '{ success userErrors { code } } }', variable_values={"id": identifier},
                )
                assert not expired.errors and expired.data["manageOpSession"]["success"] is True
            manager.filter(user_id=user.pk).update(expires_at=timezone.now() + timedelta(minutes=30))
            validate = lambda: views.op_validate(factory.post(
                "/internal/op-validate", data=json.dumps({"op_key": raw_key, "path": "/api/v1/admin/products"}),
                content_type="application/json", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
            ))
            assert json.loads(validate().content)["valid"] is True
            revoked = views.op_revoke(factory.post(
                "/internal/op-revoke", data=json.dumps({"op_key": raw_key, "reason": "logout"}),
                content_type="application/json", HTTP_X_INTERNAL_SECRET=INTERNAL_SECRET,
            ))
            assert json.loads(revoked.content)["revoked"] == 1
            assert json.loads(validate().content)["valid"] is False
            manager.filter(user_id=user.pk).update(expires_at=timezone.now() - timedelta(days=31))
            output = StringIO()
            call_command("purge_expired_opkeys", stdout=output)
            assert "1" in output.getvalue() and manager.count() == 1
            call_command("purge_expired_opkeys", apply=True, stdout=StringIO())
            assert manager.count() == 0
    finally:
        database.close()
        del connections.databases[alias]


@pytest.mark.parametrize("actor,code", [(None, "UNAUTHENTICATED"),
    (SimpleNamespace(pk=2, is_staff=False, is_superuser=False), "FORBIDDEN")])
def test_admin_session_listing_requires_administrator(actor, code):
    with patch("saleor.graphql.api._authenticated_user", return_value=actor), patch.object(
        views.OpSession.objects, "filter"
    ) as sessions:
        result = schema.execute('{ adminOpSessions { items { id } userErrors { code } } }')
    assert not result.errors
    assert result.data["adminOpSessions"]["userErrors"][0]["code"] == code
    sessions.assert_not_called()


@pytest.mark.parametrize("actor,code", [(None, "UNAUTHENTICATED"),
    (SimpleNamespace(pk=2, is_staff=False, is_superuser=False), "FORBIDDEN")])
def test_closing_session_requires_administrator(actor, code):
    with patch("saleor.graphql.api._authenticated_user", return_value=actor), patch.object(
        views.OpSession.objects, "filter"
    ) as sessions:
        result = schema.execute(
            'mutation { manageOpSession(sessionId: "11111111-2222-4333-8444-555555555555", '
            'action: "REVOKE") { success userErrors { code } } }'
        )
    assert not result.errors
    assert result.data["manageOpSession"]["success"] is False
    assert result.data["manageOpSession"]["userErrors"][0]["code"] == code
    sessions.assert_not_called()


@pytest.mark.parametrize("identifier,action", [("invalid", "REVOKE"),
    ("11111111-2222-4333-8444-555555555555", "UNKNOWN")])
def test_closing_session_rejects_invalid_input(identifier, action):
    actor = SimpleNamespace(pk=7, is_staff=True, is_superuser=False)
    with patch("saleor.graphql.api._authenticated_user", return_value=actor), patch.object(
        views.OpSession.objects, "filter"
    ) as sessions:
        result = schema.execute(
            'mutation($id: ID!, $action: String!) { manageOpSession(sessionId: $id, action: $action) '
            '{ success userErrors { code } } }', variable_values={"id": identifier, "action": action},
        )
    assert not result.errors
    assert result.data["manageOpSession"]["userErrors"][0]["code"] == "INVALID"
    sessions.assert_not_called()


@pytest.mark.parametrize("action", ["REVOKE", "EXPIRE"])
def test_admin_can_close_session_without_exposing_key(action):
    actor = SimpleNamespace(pk=7, is_staff=True, is_superuser=False)
    identifier = "11111111-2222-4333-8444-555555555555"
    session = SimpleNamespace(id=uuid.UUID(identifier), user_id=9, save=Mock())
    with patch("saleor.graphql.api._authenticated_user", return_value=actor), patch.object(
        views.OpSession.objects, "select_for_update"
    ) as sessions, patch("saleor.graphql.api.transaction.atomic", return_value=nullcontext()), patch.object(
        views.OpKeyAudit.objects, "create"
    ):
        sessions.return_value.filter.return_value.first.return_value = session
        result = schema.execute(
            'mutation($id: ID!, $action: String!) { manageOpSession(sessionId: $id, action: $action) '
            '{ success userErrors { code } } }', variable_values={"id": identifier, "action": action},
        )
    assert not result.errors
    assert result.data["manageOpSession"] == {"success": True, "userErrors": []}
    session.save.assert_called_once()