# Tasks: Wishlist (CU-C06)

**Input**: [spec.md](spec.md), [Core SDL](../schema.graphql), and [CU-C06 ODD record](../../../odd/tasks/cu-c06-wishlist.md)
**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md)

**Status**: Retrospective/as-built. All implementation tasks are checked; live PostgreSQL DDL/application is pending verification.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Parallelizable work on independent files.
- **[Story]**: Authenticated Wishlist operations and paid-order cleanup.
- All items below describe completed/as-built work; checks and commit identifiers follow the ODD record.

## Phase 1: Wishlist GraphQL and Persistence

**Purpose**: Persist owner-scoped product entries and expose authenticated operations.

- [x] T001 [US1] Define `myWishlist`, `addToWishlist`, `removeFromWishlist`, and ProductDetail `bggId` in [specs/schema.graphql](../schema.graphql).
- [x] T002 [US1] Implement owner-derived list/add/remove operations and idempotent behavior in [saleor/graphql/wishlist.py](../../saleor/graphql/wishlist.py).
- [x] T003 [US1] Remove only the paid order owner's purchased Wishlist entries from the transaction in [saleor/graphql/orders.py](../../saleor/graphql/orders.py).
- [x] T004 [US1] Cover access-token ownership, anonymous rejection, list/add/remove, idempotence, product-not-found, and payment outcomes in [tests/test_graphql_wishlist.py](../../tests/test_graphql_wishlist.py) and [tests/test_graphql_orders.py](../../tests/test_graphql_orders.py).
- [x] T005 [US1] Cover public ProductDetail identifier and GraphQL schema parity in [saleor/graphql/api.py](../../saleor/graphql/api.py) and [tests/test_graphql_products.py](../../tests/test_graphql_products.py).

## Phase 2: Verification and Delivery Evidence

**Purpose**: Record the reported Core checks and pending database validation.

- [x] T006 [US1] Record Core Wishlist/order focused tests — 29 passed — and Core full suite — 60 passed; one existing Django `USE_TZ` warning was reported.
- [x] T007 [US1] Record ProductDetail identity follow-up: focused product tests — 6 passed; full Core suite — 61 passed; SDL drift check — 1 passed.
- [x] T008 [US1] Record Core ODD work-unit commits `f089a72` (Wishlist) and `27d7e2d` (ProductDetail identity).
- [x] T009 [US1] Record live PostgreSQL DDL/application and live authenticated Wishlist/purchase-confirmation flow as pending verification.

## Dependencies & Execution Order

- T001 defines the contract for T002 and T005; T003 integrates payment-state cleanup; T004 covers operations and outcomes. T006–T009 preserve the ODD verification and delivery record.

## Implementation Strategy

Wishlist entries remain after cart addition and are removed only after successful paid confirmation. Automated tests use SQL fakes; the ODD record explicitly states that DDL was not applied to live PostgreSQL.
