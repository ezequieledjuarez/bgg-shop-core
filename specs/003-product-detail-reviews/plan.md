# Implementation Plan: Product Detail and Reviews (CU-C03)

**Branch**: `CU-06-wishlist` | **Date**: 2026-10-21 | **Spec**: [spec.md](spec.md)
**Input**: Core product/review specification in [spec.md](spec.md)

## Summary

Retrospective plan for Core product detail and review submission. `productBySlug` returns product data and approved-review aggregates; authenticated `createReview` validates rating/body/product, prevents duplicate user/product reviews through persistence constraints, and stores new submissions as pending. The public contract is in [specs/schema.graphql](../schema.graphql), and resolver behavior resides in [saleor/graphql/api.py](../../saleor/graphql/api.py).

## Technical Context

**Language/Version**: Python (exact runtime version not reported in the CU-C03 ODD record)  
**Primary Dependencies**: Django, Graphene  
**Storage**: PostgreSQL product and review records; live parser/database validation is not established by the Core ODD record  
**Testing**: pytest with Django; `tests/test_graphql_products.py` and `tests/test_graphql_reviews.py`  
**Target Platform**: Linux server  
**Project Type**: Django/Graphene web service  
**Performance Goals**: Not specified; pending verification.  
**Constraints**: Only approved reviews contribute to public detail; submissions require a verified user; one pending review per user/game; moderation operations are outside scope.  
**Scale/Scope**: Slug-based product detail and authenticated review creation.

## Constitution Check

Retrospective fit: review operations extend the existing Core GraphQL module and use the existing database access conventions. The review ODD record reports persistence SQL changes and automated tests, while PostgreSQL parser validation remained pending because `psql` was unavailable.

## Project Structure

### Documentation (this feature)

```text
specs/003-product-detail-reviews/
├── spec.md
├── plan.md
└── tasks.md
```

### Source Code (Core repository)

```text
specs/schema.graphql
saleor/graphql/api.py
tests/test_graphql_products.py
tests/test_graphql_reviews.py
```

**Structure Decision**: Product detail and review GraphQL behavior share the existing API module; the SDL and focused tests define and verify the Core boundary.

## Complexity Tracking

No moderation service or new persistence framework is in the delivered Core scope. Live PostgreSQL and parser checks remain pending where not reported as passed in the ODD record.
