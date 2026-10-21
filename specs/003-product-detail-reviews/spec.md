# Core Functional Specification: Product Detail and Reviews (As-Built)

**Status**: As-built, retrospective
**Canonical use case**: [CU-C03 — Product Detail and Reviews](../../../specs/003-product-detail-reviews-as-built/spec.md)

## Problem and objective

Core supplies product detail and review data for the end-to-end CU-C03 flow. It resolves a product by its public slug, publishes only approved reviews, and accepts authenticated review submissions into the pending moderation state.

## Core requirements and acceptance criteria

- **CORE-REV-001 — Resolve product detail**: `productBySlug` returns the available product attributes, stock, approved reviews, and a count/average derived only from approved reviews. A missing product returns `null`; an empty approved-review set has count zero and no average.
- **CORE-REV-002 — Authenticate submission**: `createReview` requires a valid authenticated user and an existing product slug; rejected requests return user errors without reporting a review as created.
- **CORE-REV-003 — Validate review input**: Core accepts ratings from 1 through 5 and trimmed, nonempty bodies up to 2,000 characters. A duplicate review for the same user and product is rejected.
- **CORE-REV-004 — Queue for moderation**: A valid new review is stored with `PENDING` status and the payload reports that status and creation time. Pending reviews are not included in the public detail result.

## GraphQL contract

- `Query.productBySlug(slug: String!): ProductDetail`
- `Mutation.createReview(input: CreateReviewInput!): CreateReviewPayload!`

`ProductDetail`, `ReviewSummary`, `PublicReview`, and review payload/error types are defined in the [Core SDL](../schema.graphql).

## Limitations and exclusions

- Core provides no moderation/approval operation or administrative workflow. Approved review publication depends on review records already having approved status.
- Product-detail resolution is implemented, but the current Core tests do not directly exercise its approved-review result; the product tests focus on catalog listing. Review-creation tests use mocked authentication/database interactions rather than a live database.
- Wishlist, manuals/rules, and buyer-facing review presentation are outside Core's evidenced responsibilities.

## Evidence

- Contract and implementation: [GraphQL SDL](../schema.graphql), [product and review resolvers](../../saleor/graphql/api.py).
- Tests: [Core product GraphQL tests](../../tests/test_graphql_products.py), [Core review GraphQL tests](../../tests/test_graphql_reviews.py).
