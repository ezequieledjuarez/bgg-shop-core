# Core Functional Specification: Catalog Search and Filters (As-Built)

**Status**: As-built, retrospective
**Canonical use case**: [CU-C02 — Catalog Search and Filters](../../../specs/002-catalog-search-filters-as-built/spec.md)

## Problem and objective

Core exposes the catalog data needed by the end-to-end CU-C02 browsing flow. It searches, filters, and paginates product rows for consumers; it does not render catalog states or determine the browser's local display order.

## Core requirements and acceptance criteria

- **CORE-CAT-001 — Browse**: `products` returns product attributes and page metadata without requiring authentication.
- **CORE-CAT-002 — Search and filter**: `products` applies an optional case-insensitive match over product name or publisher, publication year, supported player-count bounds, category identifier, and inclusive price bounds in combination.
- **CORE-CAT-003 — Paginate**: The query accepts `page` and `pageSize`, defaults to page 1 and size 20, and returns the selected page with total result and page counts.
- **CORE-CAT-004 — Reject invalid ranges**: Core rejects nonpositive page/page-size values and inverted player-count or price ranges. An unknown category identifier yields no matches under the current limited category mapping.
- **CORE-CAT-005 — Represent empty results**: A valid query with no matching products returns an empty item list and zero total; presentation of the empty state is outside Core.

## GraphQL contract

- `Query.products(page, pageSize, search, yearPublished, minPlayers, maxPlayers, categoryId, priceMin, priceMax): ProductPage`

`Product` and `ProductPage` fields are defined in the [Core SDL](../schema.graphql). No catalog-order/sort argument is exposed.

## Limitations and exclusions

- The category ID mapping is limited to three existing names; arbitrary category management is not part of this contract.
- The query's database ordering is fixed (`ORDER BY id`). Price sorting, featured/recommended ordering, relevance ranking, and catalog UI loading/error/empty presentation are not Core responsibilities.
- The resolver does not reject blank/whitespace-only search text or validate every numeric filter's domain; it applies the supplied values according to the query implementation. Any stricter boundary validation must be attributed to another layer only where evidenced.
- Tests cover product-row mapping and pagination, combined filters, and inverted ranges; they do not independently exercise every possible value or filter combination.

## Evidence

- Contract and implementation: [GraphQL SDL](../schema.graphql), [catalog resolvers](../../saleor/graphql/api.py).
- Tests: [Core product GraphQL tests](../../tests/test_graphql_products.py), including the reference-SDL parity check.
