# Implementation Plan: Checkout, Orders, and Invoice (CU-C05)

**Branch**: `CU-06-wishlist` | **Date**: 2026-10-21 | **Spec**: [spec.md](spec.md)
**Input**: Core order specification in [spec.md](spec.md)

## Summary

Retrospective plan for authenticated checkout and order lifecycle in Core. Checkout snapshots server-calculated cart lines, creates a pending order and sandbox payment preference; confirmation verifies provider data and ownership, updates payment/order state transactionally, decrements stock at most once, and creates a PDF invoice for eligible approval. Order history and the protected invoice URL are part of the Core boundary. The GraphQL contract is in [specs/schema.graphql](../schema.graphql).

## Technical Context

**Language/Version**: Python (exact runtime version not reported in the CU-C05 ODD record)  
**Primary Dependencies**: Django, Graphene, Mercado Pago Python SDK, ReportLab (the ODD record reports these runtime dependencies)  
**Storage**: PostgreSQL order, line, invoice, cart, and inventory data  
**Testing**: pytest with Django; `tests/test_graphql_orders.py` and schema parity via `tests/test_graphql_products.py`  
**Target Platform**: Linux server  
**Project Type**: Django/Graphene web service  
**Performance Goals**: Not specified; pending verification.  
**Constraints**: Sandbox only; server-side price/total; authenticated ownership; provider verification; transactional stock update; no webhooks, refunds, shipping costs, or live payment.  
**Scale/Scope**: Checkout, retry, payment confirmation, order queries, and owner-protected PDF invoice.

## Constitution Check

Retrospective fit: payment and order behavior is implemented in the existing Core GraphQL/order modules and URL configuration. Payment confirmation verifies provider data rather than trusting browser input. The ODD record reports mocked payment tests; live payment completion and full clean-database bootstrap remain pending.

## Project Structure

### Documentation (this feature)

```text
specs/005-checkout-orders-invoice/
├── spec.md
├── plan.md
└── tasks.md
```

### Source Code (Core repository)

```text
specs/schema.graphql
saleor/graphql/orders.py
saleor/graphql/payments.py
saleor/urls.py
tests/test_graphql_orders.py
tests/test_graphql_products.py
```

**Structure Decision**: Order/payment resolvers and provider integration are separated in the existing `orders.py` and `payments.py`; Django routes expose the PDF invoice view, while the SDL and tests document the GraphQL behavior.

## Complexity Tracking

No new migration framework is recorded. The whole bootstrap SQL on a clean PostgreSQL database, live sandbox payment completion, and final invoice download are explicitly unverified in the ODD record.
