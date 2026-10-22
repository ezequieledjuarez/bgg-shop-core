# Tasks: Checkout, Orders, and Invoice (CU-C05)

**Input**: [spec.md](spec.md), [Core SDL](../schema.graphql), and [CU-C05 ODD record](../../../odd/tasks/cu-c05-purchase.md)
**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md)

**Status**: Retrospective/as-built. All implementation tasks are checked; live payment and final end-to-end validation remain pending.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Parallelizable work on independent files.
- **[Story]**: Authenticated checkout and buyer-owned orders/invoices.
- All items below describe completed/as-built work; test results and commits are cited only from the ODD record.

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
- [x] T008 [US1] Record live sandbox limitations: payment approval in the browser, paid transition, stock decrement, invoice download, cart clearing, clean PostgreSQL bootstrap, and guest-cart merge remain pending verification.

## Dependencies & Execution Order

- T001 is the GraphQL contract consumed by T002; T003 supplies provider operations; T004 publishes the invoice endpoint; T005 exercises these behaviors. T006–T008 record the observed proof limits.

## Implementation Strategy

Only sandbox payment behavior is in scope. Automated payment calls are mocked in the reported Core checks; do not claim that they prove provider completion or live invoice download.
