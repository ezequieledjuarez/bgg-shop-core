# Tasks: Shopping Cart (CU-C04)

**Input**: [spec.md](spec.md), [Core SDL](../schema.graphql), and [CU-C04 ODD record](../../../odd/tasks/cu-c04-shopping-cart.md)
**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md)

**Status**: Closed for the local scope with limitations on 2026-10-30. Isolated PostgreSQL and browser merge checks passed; full clean-database bootstrap remains unverified.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Parallelizable work on independent files.
- **[Story]**: Persistent Core cart and inventory behavior.
- All items below describe completed/as-built work; checks are included only as reported.

## Phase 1: Cart Contract and Behavior

**Purpose**: Persist guest/account cart lines and expose authenticated GraphQL operations.

- [x] T001 [US1] Add inventory and cart query/mutation/payload fields to [specs/schema.graphql](../schema.graphql).
- [x] T002 [US1] Implement guest/account cart identity, cart reads and edits, current-price totals, inventory bounds, and guest-cart merge after login in [saleor/graphql/api.py](../../saleor/graphql/api.py).
- [x] T003 [US1] Cover inventory mapping and schema parity in [tests/test_graphql_products.py](../../tests/test_graphql_products.py), and cart identity, operations, stock warning, total calculation, and merge cases in [tests/test_graphql_cart.py](../../tests/test_graphql_cart.py).

## Phase 2: Verification and Limitations

**Purpose**: Preserve exactly the verification evidence and outstanding checks in the ODD record.

- [x] T004 [US1] Record focused Core tests — 12 passed — and full Core suite — 31 passed; both reported one existing Django `USE_TZ` deprecation warning.
- [x] T005 [US1] Record that Core DDL/bootstrap had been inspected but not executed against PostgreSQL in the historical ODD record; see T007 for later isolated verification.
- [x] T006 [US1] Record that guest-to-account merge was not live-verified; the ODD record reports automated merge coverage only.
- [x] T007 [US1] Verify merge in isolated PostgreSQL with an existing account cart in [tests/test_graphql_credit_postgres.py](../../tests/test_graphql_credit_postgres.py), and through login in [the browser E2E](../../../bgg-shop-frontend/tests/e2e/cart-wishlist-purchase.spec.ts). The PostgreSQL file passed 99 tests; the browser flow passed twice with simulated payment.

## Dependencies & Execution Order

- T001 defines the contract for T002; T003 verifies the query/mutation behavior. T004–T006 preserve the reported proof boundaries.

## Implementation Strategy

No stock reservation occurs during cart edits. Do not interpret test SQL fakes or separate live BFF checks as direct Core database execution. The CU-C04 ODD record reports no work-unit commit because its delivery strategy awaited a user decision.
