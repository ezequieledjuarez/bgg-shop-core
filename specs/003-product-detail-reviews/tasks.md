# Tasks: Product Detail and Reviews (CU-C03)

**Input**: [spec.md](spec.md), [Core SDL](../schema.graphql), and [CU-C03 ODD record](../../../odd/tasks/cu-c03-product-reviews.md)
**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md)

**Status**: Closed with scope limitations on 2026-10-30; see the [canonical closure](../../../specs/003-product-detail-reviews-as-built/spec.md). Checked tasks do not imply unreported verification.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Parallelizable work on independent files.
- **[Story]**: Public product detail and review submission.
- All items below describe completed/as-built work; evidence is constrained to the ODD record.

## Phase 1: Detail and Review Contract

**Purpose**: Expose public product details and authenticated review creation.

- [x] T001 [US1] Define `productBySlug`, review summary/public review fields, `CreateReviewInput`, and mutation payload in [specs/schema.graphql](../schema.graphql).
- [x] T002 [US1] Implement slug-based product detail and approved-review aggregation, plus authenticated pending review creation, in [saleor/graphql/api.py](../../saleor/graphql/api.py).
- [x] T003 [US1] Cover authentication, rating/body validation, unknown products, duplicate submission, and pending status in [tests/test_graphql_reviews.py](../../tests/test_graphql_reviews.py); cover ProductDetail identity/schema parity in [tests/test_graphql_products.py](../../tests/test_graphql_products.py).

## Phase 2: Persistence and Verification

**Purpose**: Record the as-built persistence boundary and observed checks.

- [x] T004 [US1] Record the review persistence change described by the ODD record; parser validation was absent in that historical run and was later performed in T007.
- [x] T005 [US1] Record Core verification from the ODD record: existing Core GraphQL suite — 6 tests passed. Core contract/behavior commit reported: `38aae5c` (with persistence commit `ece3221`).
- [x] T006 [US1] Use a real Django request for malformed-header tests and cover approved-review aggregation in [review tests](../../tests/test_graphql_reviews.py) and [product tests](../../tests/test_graphql_products.py), against the [Core SDL](../schema.graphql).
- [x] T007 [US1] Validate the [review DDL](../../../import_catalog.sql) with synthetic data in disposable PostgreSQL, without modifying the active database.
- [x] T008 [US1] Verify authenticated review creation, uniqueness, and pending visibility via [browser E2E](../../../bgg-shop-frontend/tests/e2e/product-reviews.spec.ts), [Core resolver](../../saleor/graphql/api.py), [Core SDL](../schema.graphql), and disposable PostgreSQL using the [review DDL](../../../import_catalog.sql).

## Dependencies & Execution Order

- T001 precedes T002; T003 exercises the resulting contract and behavior. Persistence and automated verification are recorded in T004–T005.

## Implementation Strategy

On 2026-10-30, `python -m pytest -q tests/test_graphql_reviews.py tests/test_graphql_products.py` passed (28 tests, one Django warning). PostgreSQL 15 in a disposable `--network none` container accepted the original review DDL against minimal foreign-key tables; synthetic checks covered pending default, duplicate rejection, invalid rating/body, and a missing product foreign key. The instance was removed afterward. This validates the DDL, not a live GraphQL mutation or all database scenarios. Moderation/approval remains outside Core scope.

Later on 2026-10-30, the opt-in browser test used a separate temporary Core/BFF and disposable PostgreSQL; it submitted a review through GraphQL, confirmed one `PENDING` row, rejected a duplicate, and confirmed the review was not public. Only auth, sessions, and OpKey migrations plus the product/review DDL were applied in that database. An earlier full `migrate` attempt failed on the unrelated missing `gift_card` table; the 11 GraphQL migrations were not applied there or to the active database.
