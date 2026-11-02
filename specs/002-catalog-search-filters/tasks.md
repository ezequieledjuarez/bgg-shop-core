# Tasks: Catalog Search and Filters (CU-C02)

**Input**: [spec.md](spec.md), [Core SDL](../schema.graphql), and [CU-C02 ODD record](../../../odd/tasks/cu-c02-catalog-filtering.md)
**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md)

**Status**: Closed with exceptions on 2026-10-30; see the [canonical closure](../../../specs/002-catalog-search-filters-as-built/spec.md). Checked tasks do not imply unreported verification.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Parallelizable work on independent files.
- **[Story]**: Catalog query behavior.
- All items below describe completed/as-built work; checked status does not imply unreported evidence.

## Phase 1: Catalog Query Contract and Resolver

**Purpose**: Search/filter existing catalog rows and return paginated metadata.

- [x] T001 [US1] Extend `products` arguments and product/page fields in [specs/schema.graphql](../schema.graphql).
- [x] T002 [US1] Implement parameterized search, year, player-range, category, and price predicates before count/pagination in [saleor/graphql/api.py](../../saleor/graphql/api.py).
- [x] T003 [US1] Preserve the fixed ordering and pagination metadata; retain the local three-ID category mapping without adding a migration.
- [x] T004 [US1] Cover row mapping, pagination, combined filters, and inverted ranges in [tests/test_graphql_products.py](../../tests/test_graphql_products.py).
- [x] T005 [US1] Record Core verification from the ODD record: `python -m pytest` — 4 passed with one Django `USE_TZ` warning; `python -m compileall -q saleor/graphql tests` — passed. No browser/live end-to-end validation was reported.
- [x] T006 [US1] Expose product category names and exact name filtering in [GraphQL SDL](../schema.graphql) and [catalog resolver](../../saleor/graphql/api.py), retaining legacy IDs; verify in [Core product tests](../../tests/test_graphql_products.py) and the [BFF contract](../../../bgg-shop-bff/specs/openapi.yaml).

## Dependencies & Execution Order

- T001 establishes the GraphQL contract; T002 implements it; T003 and T004 cover query semantics and behavior.
- T005 records the verification evidence from the ODD implementation record.

## Implementation Strategy

On 2026-10-30, `python -m pytest -q tests/test_graphql_products.py` passed (10 tests, one Django warning). The local browser/BFF/Core flow filtered all four categories present in the running local database; all returned products had the selected category. Relevance ordering remains unimplemented and Core query ordering remains `ORDER BY id`.
