# Core Functional Specification: Checkout, Orders, and Invoice (As-Built)

**Status**: As-built, retrospective
**Canonical use case**: [CU-C05 — Checkout, Orders, and Invoice](../../../specs/005-checkout-orders-invoice-as-built/spec.md)

## Problem and objective

Core turns an authenticated buyer's cart into an order snapshot, initiates and verifies the configured sandbox payment, maintains buyer-owned order history, and issues an invoice after eligible payment approval. Browser navigation and payment-return presentation are outside this Core scope.

## Core requirements and acceptance criteria

- **CORE-ORDER-001 — Start checkout**: `checkout` requires an authenticated buyer, a nonempty cart, available stock, and a supported shipping mode (`STANDARD` or `PICKUP`). Standard shipping requires recipient, street, city, and postal code. The order total and line values are calculated from the server-side cart and product data.
- **CORE-ORDER-002 — Initiate payment**: Checkout creates a pending-payment order snapshot and requests a payment preference from the configured sandbox integration. Disabled configuration or preference initialization failure returns an order user error.
- **CORE-ORDER-003 — Confirm or retry payment**: `confirmPayment` verifies provider payment data and order ownership before changing payment/order state. An approved payment with sufficient stock updates stock at most once, creates an invoice, and removes the purchased quantities from the cart; an approved payment without sufficient stock is represented as `paid_stock_conflict` and does not issue an invoice. Rejected, pending, and cancelled outcomes remain distinguishable. `retryPayment` accepts eligible `pending_payment` or `rejected` orders and does not revive paid orders.
- **CORE-ORDER-004 — Protect order history**: `myOrders` and `myOrder` require authentication and expose only orders owned by the authenticated buyer.
- **CORE-ORDER-005 — Expose invoice data/download**: Order GraphQL results include invoice metadata when present. The separate Core invoice endpoint returns a PDF only for an authenticated owner of a paid order with an invoice.

## GraphQL contract

- `Mutation.checkout(address, cartId, shippingMode): CheckoutPayload!`
- `Mutation.retryPayment(orderId: ID!): CheckoutPayload!`
- `Mutation.confirmPayment(orderId: ID!, paymentId: ID!): ConfirmPaymentPayload!`
- `Query.myOrders: [PurchaseOrder!]!`
- `Query.myOrder(id: ID!): PurchaseOrder`

`PurchaseOrder` contains line and invoice metadata fields. PDF download is **not** a GraphQL operation: the Core Django URL is `invoices/<uuid:order_id>/` (see [URL configuration](../../saleor/urls.py) and [invoice view](../../saleor/graphql/orders.py)).

## Limitations and exclusions

- The payment integration is sandbox-only; a live-mode payment is rejected. Provider behavior in automated tests is mocked.
- There is no provider webhook, refund handling, production payment operation, shipping-cost calculation, or shipping fulfillment. An approved payment can remain pending if the buyer never returns to confirm it; duplicate charging risk and refund handling are not solved here.
- Automated order tests cover modeled payment/order behaviors but do not verify the full browser/provider/database flow or live invoice download end to end.

## Evidence

- Contract and implementation: [GraphQL SDL](../schema.graphql), [order and payment resolvers](../../saleor/graphql/orders.py), [payment adapter](../../saleor/graphql/payments.py), [URL configuration](../../saleor/urls.py).
- Tests: [Core order GraphQL tests](../../tests/test_graphql_orders.py), including checkout, retry, confirmation, ownership, stock, and invoice cases.
