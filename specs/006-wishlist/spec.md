# CU-C06 Wishlist

## Core contract

Core stores Wishlist membership in `wishlist_entry`, keyed by the authenticated
user and product. The composite primary key prevents duplicate entries, and
foreign keys cascade when a user or catalog product is deleted. Apply this
additive DDL from the repository-level `import_catalog.sql`.

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
