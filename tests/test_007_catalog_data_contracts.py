from decimal import Decimal
import importlib
import json
import os
from pathlib import Path

import django
import yaml
from graphql import build_schema

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
django.setup()

from django.core.serializers import serialize
from saleor.promotions.models import (
    AuditLog,
    Coupon,
    CouponCounter,
    Product,
    Promotion,
    StockReservation,
)


ROOT = Path(__file__).resolve().parents[1]
BFF_OPENAPI = ROOT.parent / "bgg-shop-bff" / "specs" / "openapi.yaml"
CORE_SDL = ROOT / "specs" / "schema.graphql"


def _contract_documents():
    return yaml.safe_load(BFF_OPENAPI.read_text()), build_schema(CORE_SDL.read_text())


def test_007_openapi_contract_records_approved_product_and_coupon_rules():
    openapi, _ = _contract_documents()
    assert openapi["openapi"] == "3.2.0"

    schemas = openapi["components"]["schemas"]
    product_fields = schemas["AdminProductFields"]["properties"]
    assert product_fields["price"]["exclusiveMinimum"] == 0
    for request_name in ("AdminCreateProductRequest", "AdminUpdateProductRequest"):
        assert schemas[request_name]["allOf"][0]["$ref"] == (
            "#/components/schemas/AdminProductFields"
        )
    assert product_fields["image_url"]["pattern"].endswith(r"\.webp$")
    assert product_fields["image_file_name"]["pattern"].endswith(r"\.webp$")
    assert schemas["AdminCreateProductRequest"]["oneOf"]
    update = schemas["AdminUpdateProductRequest"]
    update_rules = update["allOf"][1]["not"]["anyOf"]
    assert {"required": ["bgg_id"]} in update_rules
    assert {"required": ["quantity"]} in update_rules
    assert {"required": ["image_url", "image_file_name"]} in update_rules

    promotion_fields = schemas["AdminPromotionFields"]["properties"]
    assert promotion_fields["discount_percent"]["minimum"] == 0
    assert promotion_fields["discount_percent"]["maximum"] == 50
    assert "product_bgg_id" in promotion_fields
    assert "active" in schemas["AdminCreatePromotionRequest"]["allOf"][1]["required"]
    assert "required" not in schemas["AdminUpdatePromotionRequest"]["allOf"][1]
    assert schemas["AdminPromotion"]["allOf"][1]["properties"]["product_bgg_id"]

    preview = openapi["paths"]["/api/v1/cart/coupons/preview"]["post"]
    assert preview["security"] == [{"BearerAuth": []}]
    assert preview["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/CouponPreviewRequest"
    )
    checkout = schemas["CheckoutRequest"]["properties"]
    assert "coupon_code" in checkout
    assert "does not reserve" in preview["description"].lower()


def test_007_graphql_sdl_contract_exposes_coupon_preview_and_promotion_product():
    _, schema = _contract_documents()
    promotion = schema.get_type("Promotion")
    assert str(schema.get_type("CreatePromotionInput").fields["active"].type) == "Boolean!"
    assert "productBggId" in promotion.fields
    assert (
        schema.get_type("UpdateProductInput").fields["price"].description
        == "Must be strictly greater than zero."
    )
    stock_description = " ".join(
        schema.mutation_type.fields["adjustProductStock"].description.split()
    )
    assert "Requires an active staff or superuser account." in stock_description
    assert "previewCoupon" in schema.query_type.fields
    assert schema.get_type("CouponPreview").fields["valid"]
    assert "couponCode" in schema.mutation_type.fields["checkout"].args
    assert "checkoutKey" in schema.mutation_type.fields["checkout"].args
    assert "case-sensitive equality" in schema.get_type("Coupon").fields["code"].description
    assert "exclusive" in schema.get_type("Coupon").fields["endsAt"].description.lower()


def test_007_graphql_sdl_defines_flattened_admin_coupon_contract():
    _, schema = _contract_documents()
    coupon_fields = schema.get_type("AdminCoupon").fields

    assert str(coupon_fields["id"].type) == "Int!"
    assert str(coupon_fields["code"].type) == "String!"
    assert str(coupon_fields["discountPercent"].type) == "Float!"
    assert str(coupon_fields["remaining"].type) == "BigInt!"
    assert str(coupon_fields["active"].type) == "Boolean!"
    assert str(coupon_fields["startsAt"].type) == "String"
    assert str(coupon_fields["endsAt"].type) == "String"
    assert "product" not in coupon_fields
    assert "discountPct" not in schema.get_type("Coupon").fields

    create_fields = schema.get_type("AdminCreateCouponInput").fields
    assert {"code", "discountPercent", "remaining", "active"} <= set(create_fields)
    assert {
        name for name, field in create_fields.items() if str(field.type).endswith("!")
    } >= {"code", "discountPercent", "remaining", "active"}
    update_fields = schema.get_type("AdminUpdateCouponInput").fields
    assert "code" not in update_fields
    assert {"discountPercent", "remaining", "active", "startsAt", "endsAt"} <= set(
        update_fields
    )
    assert "adminCoupons" in schema.query_type.fields
    assert "createAdminCoupon" in schema.mutation_type.fields
    assert "updateAdminCoupon" in schema.mutation_type.fields
    assert "audit" in schema.mutation_type.fields["createAdminCoupon"].description.lower()
    assert "audit" in schema.mutation_type.fields["updateAdminCoupon"].description.lower()


def test_007_graphene_runtime_matches_admin_coupon_sdl_shape():
    from saleor.graphql.api import schema as runtime_schema

    runtime = runtime_schema.graphql_schema
    expected = build_schema(CORE_SDL.read_text())
    for type_name in ("AdminCoupon", "AdminCouponPayload", "AdminCreateCouponInput", "AdminUpdateCouponInput"):
        expected_fields = expected.get_type(type_name).fields
        runtime_fields = runtime.get_type(type_name).fields
        assert {
            name: str(field.type) for name, field in runtime_fields.items()
        } == {name: str(field.type) for name, field in expected_fields.items()}
    for name in ("adminCoupons",):
        assert str(runtime.get_type("Query").fields[name].type) == str(
            expected.get_type("Query").fields[name].type
        )
    for name in ("createAdminCoupon", "updateAdminCoupon"):
        runtime_field = runtime.get_type("Mutation").fields[name]
        expected_field = expected.get_type("Mutation").fields[name]
        assert str(runtime_field.type) == str(expected_field.type)
        assert {
            arg: str(value.type) for arg, value in runtime_field.args.items()
        } == {
            arg: str(value.type) for arg, value in expected_field.args.items()
        }


def test_007_coupon_contract_uses_approved_whole_peso_rounding():
    openapi, schema = _contract_documents()
    graphql_description = schema.get_type("CouponPreview").description.lower()
    preview_description = openapi["paths"]["/api/v1/cart/coupons/preview"]["post"][
        "description"
    ].lower()
    assert "round each total upward once" in graphql_description
    assert "ties favor the store promotion" in graphql_description
    assert "round each total upward once" in preview_description
    assert "ties favor the store promotion" in preview_description
    assert "truncated" not in graphql_description
    assert "truncated" not in preview_description
    preview_type = schema.get_type("CouponPreview")
    assert str(preview_type.fields["couponCode"].type) == "String!"
    assert str(preview_type.fields["discountAmount"].type) == "Float!"
    assert str(preview_type.fields["newTotal"].type) == "Float!"
    assert openapi["components"]["schemas"]["CouponPreviewResponse"]["properties"][
        "coupon_code"
    ].get("nullable") is not True


def test_007_coupon_reservation_migration_is_registered_and_non_destructive():
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(None)
    migration = importlib.import_module(
        "saleor.promotions.migrations.0004_coupon_checkout_reservations"
    ).Migration
    assert ("promotions", "0004_coupon_checkout_reservations") in loader.disk_migrations
    assert migration.dependencies == [("promotions", "0003_stock_reservation_constraints")]
    sql = "\n".join(operation.sql for operation in migration.operations).lower()
    assert "create table coupon_checkout_reservation" in sql
    assert "operation_key_hash char(64) not null unique" in sql
    assert "reserved" in sql and "consumed" in sql and "released" in sql


def test_007_openapi_requires_staff_auth_for_admin_and_documents_stock_retry():
    openapi, _ = _contract_documents()
    paths = openapi["paths"]
    assert paths["/api/v1/admin/products"]["post"]["security"] == [{"BearerAuth": []}]
    stock = paths["/api/v1/admin/products/{product_id}/stock"]["post"]
    assert stock["security"] == [{"BearerAuth": []}]
    description = stock["description"].lower()
    assert "partial-to-zero" in description
    assert "successful movement identifiers do not expire" in description


def test_007_persistence_models_and_forward_migration_encode_data_constraints():
    assert Product._meta.db_table == "product_boardgame"
    assert Product._meta.managed is False
    assert Product._meta.get_field("bgg_id").unique is True
    assert Product._meta.get_field("price_amount").decimal_places == 2
    assert {constraint.name for constraint in Product._meta.constraints} >= {
        "product_price_positive",
        "product_boardgame_quantity_check",
    }
    assert Product._meta.get_field("name").unique is True
    reservation = StockReservation._meta
    assert reservation.get_field("bgg_id").remote_field.model is Product
    assert reservation.get_field("bgg_id").remote_field.field_name == "bgg_id"
    assert {c.name for c in reservation.constraints} == {"stock_reservation_reserved_qty_check"}
    assert Promotion._meta.get_field("product").remote_field.model is Product
    assert {constraint.name for constraint in Promotion._meta.constraints} >= {
        "promotion_discount_range",
    }
    assert {constraint.name for constraint in Coupon._meta.constraints} >= {
        "coupon_remaining_nonnegative",
        "coupon_valid_period",
    }
    assert Coupon._meta.get_field("code").unique is True
    assert "discount_pct" not in {
        field.name for field in Coupon._meta.get_fields()
    }
    assert CouponCounter._meta.db_table == "coupon_counter"
    assert AuditLog._meta.db_table == "audit_log"
    assert {"actor", "action", "diff", "created_at"} <= {
        field.name for field in AuditLog._meta.fields
    }

    migration = importlib.import_module(
        "saleor.graphql.migrations.0017_007_contract_data_dependencies"
    ).Migration
    assert ("graphql", "0016_coupon_constraints") in migration.dependencies
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(None)
    loader.graph.validate_consistency()
    assert ("graphql", "0017_007_contract_data_dependencies") in loader.disk_migrations
    sql = "\n".join(operation.sql for operation in migration.operations).lower()
    assert "price_amount > 0" in sql
    assert "discount_pct >= 0" in sql and "discount_pct <= 50" in sql
    assert "product_bgg_id" in sql
    assert "where active" in sql and "unique" in sql
    assert "not valid" in sql
    assert "product_quantity_nonnegative" not in sql
    assert sql.count("validate constraint") == 2


def test_007_promotion_activation_migration_is_discovered_after_initial_migration():
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(None)
    initial = ("promotions", "0001_initial")
    activation = ("graphql", "0014_promotion_manual_activation")

    assert initial in loader.disk_migrations
    assert activation in loader.disk_migrations
    assert initial in loader.disk_migrations[activation].dependencies
    loader.graph.validate_consistency()
    plan = loader.graph.forwards_plan(activation)
    assert plan.index(initial) < plan.index(activation)


def test_007_product_promotion_and_coupon_models_serialize_contract_fields():
    product = Product(
        id=1,
        bgg_id=274364,
        name="Local game",
        price_amount=Decimal("25.00"),
        image_url="/media/local-game.webp",
        image_filename="local-game.webp",
        quantity=3,
    )
    promotion = Promotion(
        id=2,
        name="Local discount",
        active=True,
        discount_pct=Decimal("10.00"),
        product=product,
    )
    coupon = Coupon(
        id=3,
        code="SAVE10",
        promotion=promotion,
        remaining=2,
        active=True,
    )

    product_fields = json.loads(serialize("json", [product]))[0]["fields"]
    promotion_fields = json.loads(serialize("json", [promotion]))[0]["fields"]
    coupon_fields = json.loads(serialize("json", [coupon]))[0]["fields"]
    assert product_fields["price_amount"] == "25.00"
    assert promotion_fields["product"] == product.bgg_id
    assert promotion_fields["discount_pct"] == "10.00"
    assert coupon_fields["code"] == "SAVE10"
    assert coupon_fields["remaining"] == 2


def test_007_latest_migration_state_matches_promotion_models():
    from django.apps import apps
    from django.db.migrations.autodetector import MigrationAutodetector
    from django.db.migrations.loader import MigrationLoader
    from django.db.migrations.state import ProjectState

    loader = MigrationLoader(None)
    loader.graph.validate_consistency()
    sync = ("promotions", "0002_007_state_sync")
    assert sync in loader.disk_migrations
    assert set(loader.disk_migrations[sync].dependencies) >= {
        ("promotions", "0001_initial"),
        ("graphql", "0017_007_contract_data_dependencies"),
    }
    plan = loader.graph.forwards_plan(sync)
    assert plan.index(("graphql", "0017_007_contract_data_dependencies")) < plan.index(sync)

    changes = MigrationAutodetector(
        loader.project_state(), ProjectState.from_apps(apps)
    ).changes(loader.graph)
    assert changes.get("promotions", []) == []


def test_007_audit_read_contract_matches_openapi_and_core_sdl():
    openapi, sdl = _contract_documents()
    operation = openapi["paths"]["/api/v1/admin/audit"]["get"]
    parameters = {item["name"]: item for item in operation["parameters"]}
    assert set(parameters) == {
        "entity_type", "entity_id", "actor_id", "action",
        "created_from", "created_to", "cursor", "limit",
    }
    assert parameters["limit"]["schema"] == {
        "type": "integer", "minimum": 1, "maximum": 100, "default": 50,
    }
    assert set(operation["responses"]) == {"200", "400", "401", "403", "502"}
    assert operation["security"] == [{"BearerAuth": []}]
    assert "created_at DESC, id DESC" in operation["description"]
    assert "[REDACTED]" in operation["description"]

    schemas = openapi["components"]["schemas"]
    record = schemas["AdminAuditRecord"]
    assert set(record["required"]) == {
        "id", "actor_id", "action", "entity_type", "entity_id", "created_at", "changes",
    }
    assert set(record["properties"]["changes"]["required"]) == {"before", "after"}
    assert set(schemas["AdminAuditPage"]["required"]) == {"items", "next_cursor"}

    query = sdl.get_type("Query").fields["adminAuditLogs"]
    assert str(query.type) == "AdminAuditPage!"
    assert {name: str(arg.type) for name, arg in query.args.items()} == {
        "entityType": "String", "entityId": "String", "actorId": "String",
        "action": "String", "createdFrom": "String", "createdTo": "String",
        "cursor": "String", "limit": "Int",
    }
    assert query.args["limit"].default_value == 50


def test_007_graphene_runtime_matches_audit_read_sdl():
    from saleor.graphql.api import schema as runtime_schema

    runtime = runtime_schema.graphql_schema
    expected = build_schema(CORE_SDL.read_text())
    for type_name in ("AdminAuditRecord", "AdminAuditPage"):
        assert {
            name: str(field.type) for name, field in runtime.get_type(type_name).fields.items()
        } == {
            name: str(field.type) for name, field in expected.get_type(type_name).fields.items()
        }
    runtime_field = runtime.get_type("Query").fields["adminAuditLogs"]
    expected_field = expected.get_type("Query").fields["adminAuditLogs"]
    assert str(runtime_field.type) == str(expected_field.type)
    assert {
        name: (str(arg.type), arg.default_value) for name, arg in runtime_field.args.items()
    } == {
        name: (str(arg.type), arg.default_value) for name, arg in expected_field.args.items()
    }
