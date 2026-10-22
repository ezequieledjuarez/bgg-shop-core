# Implementation Plan: Catalog Search and Filters (CU-C02)

**Branch**: `CU-06-wishlist` | **Date**: 2026-10-21 | **Spec**: [spec.md](spec.md)
**Input**: Core catalog specification in [spec.md](spec.md)

## Summary

Retrospective plan for Core's paginated `products` GraphQL query. The query applies optional search, year, supported-player-range, category, and inclusive price filters to existing `product_boardgame` columns before counting and pagination. The API preserves product mapping and page metadata, uses parameterized SQL, and exposes its contract in [specs/schema.graphql](../schema.graphql).

## Technical Context

**Language/Version**: Python (exact runtime version not reported in the CU-C02 ODD record)  
**Primary Dependencies**: Django, Graphene  
**Storage**: Existing PostgreSQL `product_boardgame` data via Django database connection; no schema migration is reported  
**Testing**: pytest with Django; `tests/test_graphql_products.py`  
**Target Platform**: Linux server  
**Project Type**: Django/Graphene web service  
**Performance Goals**: Not specified; pending verification.  
**Constraints**: Parameterized SQL; filters precede count/limit/offset; fixed `ORDER BY id`; the category map is limited to three local IDs; relevance ordering is not implemented.  
**Scale/Scope**: One public catalog query with optional filters and pagination.

## Constitution Check

Retrospective fit: the implementation extends the existing `products` query and uses existing database columns, avoiding a new query abstraction and migration framework. No claim is made that unreported performance or live-database checks passed.

## Project Structure

### Documentation (this feature)

```text
specs/002-catalog-search-filters/
├── spec.md
├── plan.md
└── tasks.md
```

### Source Code (Core repository)

```text
specs/schema.graphql
saleor/graphql/api.py
tests/test_graphql_products.py
```

**Structure Decision**: Catalog fields and resolver behavior are maintained in the existing Core GraphQL module, with the reference SDL and focused product query tests alongside it.

## Complexity Tracking

No added persistence layer is reported. Relevance ranking, a category table, and UI pagination are out of scope for the delivered Core behavior and remain limitations rather than implied implementation work.
