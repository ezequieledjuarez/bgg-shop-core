# Tasks (Core): CU-A02 — Sales Metrics & Reports

Plan and rules: `../../specs/010-sales-metrics-reports/plan.md`. Global ledger: `../../specs/010-sales-metrics-reports/tasks.md`.
Branch: `010-sales-metrics-reports`. Local validation only; disposable PostgreSQL for integration tests; do not migrate an active database.

## Contract (blocking)

- [x] X-1 Add `adminSalesMetrics(from, to, categories, productIds)` and types (`SalesMetrics`, `TopProduct`, `RevenueByDay`, `StatusCount`) to `specs/schema.graphql`. Document staff/superuser access; timezone-aware UTC instants and `[from, to)`; revenue/`ordersCount` by `paid_at`; status counts by `created_at`; gross line-subtotal revenue; exact filters with matching orders counted once; dispatch boundaries; `UNAUTHENTICATED`/`FORBIDDEN`/`INVALID` with `extensions.field`; maximum 366 days; and last-30-days default when both bounds are omitted.
- [x] X-5 Document paired `from`/`to` semantics in the SDL and plan: callers provide both bounds or omit both; a one-sided range returns `INVALID` with `extensions.field` naming the missing bound; omitting both defaults to the last 30 days.
- [x] X-3 Add `adminSalesCategories` (distinct stored `product_boardgame.category` values) to the SDL with staff/superuser access and documented `UNAUTHENTICATED`/`FORBIDDEN` authorization errors.

## Implementation

- [x] C-1 Write failing tests in `tests/test_admin_sales_metrics_postgres.py`: `[from, to)` boundaries; only `paid` counts as revenue; subtotal-based revenue; exact category and product filters independently and together; order with ≥1 matching line counted once, revenue only from matching lines; top-10 order (units, revenue, bgg_id); `revenueByDay` in UTC (empty-day filling remains unspecified); dispatch boundaries 23h59m/24h/71h59m/72h; pending payment mapping; rejected/cancelled/`paid_stock_conflict` excluded from dispatch counts; payment and dispatch counts by `created_at`; empty range; invalid timezone/range, >366 days, `from >= to`; 401/403; distinct stored categories and category authorization. (after X-1; expected RED observed)
- [x] C-1a Add GraphQL tests for `from`-only (expect missing `to`) and `to`-only (expect missing `from`), explicitly passing `None` for the omitted bound. Keep both-omitted default-range coverage unchanged; observe expected RED before C-2.
- [x] C-2 Implement `adminSalesMetrics` resolver in `saleor/graphql/api.py`: staff/superuser guard, validation, parameterized SQL, and Decimal database results through Graphene serialization. Metrics and filter cases in C-1/C-1a pass; category-query cases remain for C-3.
- [ ] C-3 Implement `adminSalesCategories` resolver with test. (after X-3)
- [ ] C-4 Review `EXPLAIN` on seeded data; add an index migration only if justified, as a separate reviewed step. Record the outcome either way.
- [ ] C-5 Run Core tests for touched areas; record results in the global ledger.

## C-1 Evidence

- `python -m py_compile tests/test_admin_sales_metrics_postgres.py`: PASS.
- `pytest -q tests/test_admin_sales_metrics_postgres.py`: collection error, `ModuleNotFoundError: No module named 'saleor'`; this entrypoint did not include the Core repository root on the import path.
- `PYTHONPATH=. pytest -q tests/test_admin_sales_metrics_postgres.py`: 18 skipped, 1 warning. `SALES_METRICS_TEST_POSTGRES_DSN` was absent at this earlier check; the fixture requires a loopback PostgreSQL database and matching unique `sales_metrics_test_<uuid>` role/database.
- `SALES_METRICS_TEST_POSTGRES_DSN=<ephemeral loopback PostgreSQL 15 database> PYTHONPATH=. python -m pytest -q -p no:cacheprovider --tb=short tests/test_admin_sales_metrics_postgres.py`: 18 failed as expected because Graphene `Query` does not yet expose `adminSalesMetrics` or `adminSalesCategories`; no test reached a resolver. Container cleanup verified; no disposable test container remains.
- X-5: PyYAML/OpenAPI and graphql-core/SDL parses and paired-bound assertions passed; both query parameters remain individually optional.
- C-1a: `python -m py_compile tests/test_admin_sales_metrics_postgres.py` passed. The focused PostgreSQL run produced the expected RED: 20 failed, 1 warning in 5.35s because Graphene does not expose `adminSalesMetrics` or `adminSalesCategories`; no collection or database setup errors occurred. The fresh loopback-only container was removed and its absence verified. No resolver behavior was implemented or passed.
- C-1a parent rerun: 20 failed, 1 warning in 5.23s against a fresh Podman PostgreSQL container, reproducing the expected missing-field behavior; cleanup passed. A delegated execution verifier reported 20 errors without a traceback; the parent rerun immediately afterward collected and ran all cases successfully to expected RED.

## C-2 Evidence

- `PYTHONPATH=. python -m pytest -q -p no:cacheprovider --tb=short tests/test_admin_sales_metrics_postgres.py -k 'not categories'` against fresh loopback-only Podman PostgreSQL 15: PASS, 17 passed, 4 deselected, 1 Django `USE_TZ` deprecation warning in 4.25s. The temporary container was explicitly removed and its absence verified; the active `saleor` database was not used.
- `python -m py_compile saleor/graphql/api.py`: PASS.
- `git diff --check`: PASS.
- Parent rerun of the C-2 focused metrics/filter suite: PASS, 17 passed and 4 deselected, 1 Django warning in 4.20s. The exit trap left the uniquely named container running; the parent removed it explicitly and verified it was absent.
- `PYTHONPATH=. python -m pytest -q -p no:cacheprovider --tb=short tests/test_graphql_products.py::test_generated_schema_matches_reference_sdl`: FAIL because runtime still lacks `adminSalesCategories`, reserved for C-3. Targeted comparison confirmed this is the only missing Query field; `adminSalesMetrics` type, arguments, payload fields, and description match the SDL.
- C-2 work-unit commits: Core `b447261a93d6f127963646ea24874ea9a9eb487d`; orchestrator `717be40568313763f6c09f94f64aa8455871fe6c`.
