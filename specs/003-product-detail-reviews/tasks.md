# Tasks: Product Detail and Reviews (CU-C03)

**Input**: [spec.md](spec.md), [Core SDL](../schema.graphql), and [CU-C03 ODD record](../../../odd/tasks/cu-c03-product-reviews.md)
**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md)

**Status**: Retrospective/as-built. All tasks are checked; outstanding database validation is stated explicitly.

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

- [x] T004 [US1] Record the review persistence change described by the ODD record; PostgreSQL parser validation remains pending because `psql` was unavailable.
- [x] T005 [US1] Record Core verification from the ODD record: existing Core GraphQL suite — 6 tests passed. Core contract/behavior commit reported: `38aae5c` (with persistence commit `ece3221`).

## Dependencies & Execution Order

- T001 precedes T002; T003 exercises the resulting contract and behavior. Persistence and automated verification are recorded in T004–T005.

## Implementation Strategy

Moderation/approval is outside this Core scope. Do not interpret mocked review tests or the reported existing suite as live PostgreSQL verification; database parser validation remains pending.
