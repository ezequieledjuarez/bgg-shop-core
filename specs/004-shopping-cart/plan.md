# Implementation Plan: Shopping Cart (CU-C04)

**Branch**: `CU-06-wishlist` | **Date**: 2026-10-21 | **Spec**: [spec.md](spec.md)
**Input**: Core cart specification in [spec.md](spec.md)

## Summary

Retrospective plan for Core's server-persisted guest and account carts. GraphQL operations read, add, update, and remove cart lines; ownership comes from the authenticated user or opaque guest cart identity. Totals use current product prices, requests are bounded by inventory, and login merges guest lines into the account cart. The contract is in [specs/schema.graphql](../schema.graphql), with behavior in [saleor/graphql/api.py](../../saleor/graphql/api.py).

## Technical Context

**Language/Version**: Python (exact runtime version not reported in the CU-C04 ODD record)  
**Primary Dependencies**: Django, Graphene  
**Storage**: PostgreSQL cart, cart-line, and product inventory records; ODD reports DDL inspection but no direct PostgreSQL execution for Core verification  
**Testing**: pytest with Django; `tests/test_graphql_cart.py` and `tests/test_graphql_products.py`  
**Target Platform**: Linux server  
**Project Type**: Django/Graphene web service  
**Performance Goals**: Not specified; pending verification.  
**Constraints**: Cart edits do not reserve stock; account identity is token-derived; guest merge caps combined quantity to available stock and removes zero-stock lines.  
**Scale/Scope**: Cart query/mutations, inventory exposure, and login handoff.

## Constitution Check

Retrospective fit: Core extends the existing GraphQL and SQL access patterns and avoids adding a migration framework. The ODD record reports focused/full automated checks and live BFF cart checks, but explicitly says the Core DDL was not executed against PostgreSQL.

## Project Structure

### Documentation (this feature)

```text
specs/004-shopping-cart/
├── spec.md
├── plan.md
└── tasks.md
```

### Source Code (Core repository)

```text
specs/schema.graphql
saleor/graphql/api.py
tests/test_graphql_cart.py
tests/test_graphql_products.py
```

**Structure Decision**: Cart resolvers and catalog inventory mapping are implemented in the existing GraphQL API module; `schema.graphql` and the focused tests capture the Core contract and behavior.

## Complexity Tracking

No separate cart repository layer is reported. PostgreSQL DDL/bootstrap execution and live guest-to-account merge remain pending; they are not inferred from mocked SQL tests.
