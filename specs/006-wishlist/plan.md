# Implementation Plan: Wishlist (CU-C06)

**Branch**: `CU-06-wishlist` | **Date**: 2026-10-21 | **Spec**: [spec.md](spec.md)
**Input**: Core Wishlist specification in [spec.md](spec.md)

## Summary

Retrospective plan for persistent, account-owned Wishlist operations in Core. Authenticated callers can list, add, and remove products without supplying a user identifier. Product detail exposes the public BGG ID for downstream consumers. On successful paid confirmation, Core removes only the purchased user's matching Wishlist entries in the order transaction; unpaid outcomes preserve them. The GraphQL contract is in [specs/schema.graphql](../schema.graphql).

## Technical Context

**Language/Version**: Python (exact runtime version not reported in the CU-C06 ODD record)  
**Primary Dependencies**: Django, Graphene  
**Storage**: PostgreSQL `wishlist_entry` records, with ownership/product constraints described by the as-built spec  
**Testing**: pytest with Django; `tests/test_graphql_wishlist.py`, `tests/test_graphql_orders.py`, and `tests/test_graphql_products.py`  
**Target Platform**: Linux server  
**Project Type**: Django/Graphene web service  
**Performance Goals**: Not specified; pending verification.  
**Constraints**: Owner is derived from verified access token; add/remove are idempotent; purchased entries are removed only after paid approval; migration/DDL execution against live PostgreSQL is not verified.  
**Scale/Scope**: Wishlist GraphQL operations, ProductDetail identity field, and payment-confirmation integration.

## Constitution Check

Retrospective fit: the work reuses Core product/authentication patterns and integrates cleanup into the existing order transaction. The ODD record reports automated unit/schema checks and explicitly leaves the live PostgreSQL DDL/application flow unverified.

## Project Structure

### Documentation (this feature)

```text
specs/006-wishlist/
├── spec.md
├── plan.md
└── tasks.md
```

### Source Code (Core repository)

```text
specs/schema.graphql
saleor/graphql/wishlist.py
saleor/graphql/orders.py
saleor/graphql/api.py
tests/test_graphql_wishlist.py
tests/test_graphql_orders.py
tests/test_graphql_products.py
```

**Structure Decision**: Wishlist persistence resolvers are in `saleor/graphql/wishlist.py`; payment confirmation and ProductDetail integration use the existing `orders.py` and `api.py` modules. The shared SDL and targeted test files describe the service contract.

## Complexity Tracking

No new abstraction framework is recorded. Live PostgreSQL DDL/application and full authenticated live-flow verification remain pending, as reported in the ODD record.
