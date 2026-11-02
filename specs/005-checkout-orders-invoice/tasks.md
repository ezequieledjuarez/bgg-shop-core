# Tasks: Checkout, Orders, and Invoice (CU-C05)

**Input**: [spec.md](spec.md), [Core SDL](../schema.graphql), and [CU-C05 ODD record](../../../odd/tasks/cu-c05-purchase.md)
**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md)

**Status**: Closed for the local, non-production scope with an accepted exception on 2026-10-30. T010 remains open; the real provider round trip and complete clean-database bootstrap remain unverified.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Parallelizable work on independent files.
- **[Story]**: Authenticated checkout and buyer-owned orders/invoices.
- Phase 1 describes implemented work; later verification is recorded separately, and T010 remains open as the accepted exception.

## Phase 1: Order and Payment Contract

**Purpose**: Create order snapshots and safely confirm sandbox payments.

- [x] T001 [US1] Define checkout, retry, confirmation, order query, line, invoice, address, and shipping types in [specs/schema.graphql](../schema.graphql).
- [x] T002 [US1] Implement checkout, ownership-scoped order reads, retry and payment confirmation in [saleor/graphql/orders.py](../../saleor/graphql/orders.py).
- [x] T003 [US1] Implement sandbox configuration, currency mapping, preference creation, and provider payment lookup in [saleor/graphql/payments.py](../../saleor/graphql/payments.py).
- [x] T004 [US1] Expose the owner-protected PDF invoice view through [saleor/urls.py](../../saleor/urls.py) and the implementation in [saleor/graphql/orders.py](../../saleor/graphql/orders.py).
- [x] T005 [US1] Cover checkout, mocked payment outcomes, idempotence, stock conflicts, ownership, retry, and invoice response in [tests/test_graphql_orders.py](../../tests/test_graphql_orders.py); use the SDL parity test in [tests/test_graphql_products.py](../../tests/test_graphql_products.py).

## Phase 2: Verification and Pending Live Flow

**Purpose**: Record ODD-reported checks and preserve unverified operational steps.

- [x] T006 [US1] Record Core verification from the ODD record after the currency fix: full Core suite — 51 passed; the currency-code regression test was added.
- [x] T007 [US1] Record that generated-schema introspection comparison passed without SDL drift, as reported in the ODD record.
- [x] T008 [US1] Record the historical live sandbox limitations. Later simulated-provider verification is recorded in [the integration record](../../../odd/tasks/cart-checkout-wishlist-validation.md); the real provider round trip and clean PostgreSQL bootstrap remain unverified.
- [x] T009 [US1] Verify repeated simulated confirmation of rejected, pending, stock-conflict, and paid outcomes in disposable PostgreSQL; [tests/test_graphql_credit_postgres.py](../../tests/test_graphql_credit_postgres.py) checks stock, invoice, cart, and Wishlist effects. Core order/PostgreSQL suites: 182 passed and one expected failure on 2026-10-30.
- [ ] T010 [US1] Resolve the documented second distinct approved payment for the same preference: accepted as an explicit exception for local, non-production closure, not completed. The PostgreSQL regression in [tests/test_graphql_credit_postgres.py](../../tests/test_graphql_credit_postgres.py) is marked strict expected failure because Core does not return `PAYMENT_REQUIRES_REVIEW`. The opt-in [browser E2E](../../../bgg-shop-frontend/tests/e2e/cart-wishlist-purchase.spec.ts) also failed on 2026-10-30: HTTP 200 instead of 409, with no review record in disposable PostgreSQL. Do not treat this as prevention of a provider charge.

## Dependencies & Execution Order

- T001 is the GraphQL contract consumed by T002; T003 supplies provider operations; T004 publishes the invoice endpoint; T005 exercises these behaviors. T006–T008 record the observed proof limits.

## Implementation Strategy

Only sandbox payment behavior is in scope. Automated payment calls are mocked in the reported Core checks; do not claim that they prove provider completion or live invoice download.
