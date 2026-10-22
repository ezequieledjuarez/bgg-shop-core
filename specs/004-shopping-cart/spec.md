# Core Functional Specification: Shopping Cart (As-Built)

**Status**: As-built, retrospective
**Canonical use case**: [CU-C04 — Shopping Cart](../../../specs/004-shopping-cart-as-built/spec.md)

## Problem and objective

Core provides server-persisted guest and account carts for the end-to-end CU-C04 flow. It owns cart identity, line updates, stock bounds, and current-price totals; browser cart presentation and hiding the opaque guest identifier belong to other layers.

## Core requirements and acceptance criteria

- **CORE-CART-001 — Read a cart**: `cart` returns its identifier, lines, current unit prices, line subtotals, current inventory quantities, and cart subtotal. Authenticated cart identity is derived from the validated access token, not a client-supplied account ID.
- **CORE-CART-002 — Edit lines**: `addToCart`, `updateCartQuantity`, and `removeFromCart` add, change, or remove a product line and return the recalculated cart. Quantities must be positive for add/update operations.
- **CORE-CART-003 — Enforce available stock**: An add/update request above current inventory is capped and returns an `INSUFFICIENT_STOCK` user warning; unknown products and invalid quantities return user errors.
- **CORE-CART-004 — Merge after login**: When `login` receives a valid guest cart ID, Core merges its lines into the account cart, sums matching quantities up to current inventory, removes zero-stock outcomes, and deletes the merged guest cart.
- **CORE-CART-005 — Do not reserve stock**: Cart reads and edits do not decrement inventory. Cart line prices and totals use current product prices rather than a cart-time price snapshot.

## GraphQL contract

- `Query.cart(cartId: ID): Cart!`
- `Mutation.addToCart(bggId: Int!, cartId: ID, quantity: Int!): CartPayload!`
- `Mutation.updateCartQuantity(bggId: Int!, cartId: ID, quantity: Int!): CartPayload!`
- `Mutation.removeFromCart(bggId: Int!, cartId: ID): CartPayload!`
- Related handoff: `Mutation.login(input: LoginInput!): LoginPayload!` accepts optional `guestCartId`.

Cart and error fields are defined in the [Core SDL](../schema.graphql).

## Limitations and exclusions

- Core returns an opaque guest cart ID in the GraphQL contract; the BFF/UI are responsible for the canonical behavior of not exposing that identifier in the user interface.
- Cart editing does not reserve stock. Inventory may change before checkout, where stock is checked again.
- Cart tests exercise the behavior using test database/cursor fakes; they do not establish live-stack or browser behavior.

## Evidence

- Contract and implementation: [GraphQL SDL](../schema.graphql), [cart and login resolvers](../../saleor/graphql/api.py).
- Tests: [Core cart GraphQL tests](../../tests/test_graphql_cart.py), including guest/account association, stock capping, edits, and login merge.
