# CU-C06 Wishlist

**Status**: Closed for the local scope with limitations on 2026-10-30; see the [canonical spec](../../../specs/006-wishlist-as-built/spec.md).

## Core contract

`ProductDetail` exposes the public BoardGameGeek product identifier as
`bggId: Int!`. The BFF uses this value as `bgg_id` in its product-detail
response so the Frontend can add the product to Wishlist from either catalog
navigation or a direct detail route.

Core expects Wishlist membership in `wishlist_entry`, keyed by the authenticated
user and product. The DDL in [import_catalog.sql](../../../import_catalog.sql)
defines a composite primary key and foreign keys that cascade when a user or
product is deleted. Its creation, repeated application, duplicate handling,
foreign keys, and cascades were checked on disposable PostgreSQL on 2026-10-30.
It has not been applied to the active database; `IF NOT EXISTS` does not repair
an existing table with different constraints. On 2026-10-30, the authenticated
save and purchase cleanup passed in [browser E2E](../../../bgg-shop-frontend/tests/e2e/cart-wishlist-purchase.spec.ts)
against disposable PostgreSQL, with the payment provider simulated.

The GraphQL API exposes:

- `myWishlist: [Product!]!` to list the caller's saved products.
- `addToWishlist(bggId: Int!): Product!` to save a product idempotently.
- `removeFromWishlist(bggId: Int!): Product!` to remove a product idempotently.

All three operations derive ownership from the verified access token. They do
not accept a user identifier. Anonymous requests fail with a GraphQL
authentication error; unknown products fail with a GraphQL product-not-found
error. Wishlist results reuse the catalog `Product` fields.

Adding an item to the cart does not modify Wishlist membership. During payment
confirmation, Core removes only purchased products for the order owner, and
only in the same transaction that marks the order paid. Pending or rejected
payments and approved payments that cannot complete because of insufficient
stock leave Wishlist membership unchanged.

## Verification

Focused coverage is in `tests/test_graphql_wishlist.py` and
`tests/test_graphql_orders.py`. The catalog product test also compares the
generated Graphene schema against `specs/schema.graphql`.
