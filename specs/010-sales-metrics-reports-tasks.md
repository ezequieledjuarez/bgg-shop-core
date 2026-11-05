# Tasks (Core): CU-A02 — Sales Metrics & Reports

Plan and rules: `../../specs/010-sales-metrics-reports/plan.md`. Global ledger: `../../specs/010-sales-metrics-reports/tasks.md`.
Branch: `010-sales-metrics-reports`. Local validation only; disposable PostgreSQL for integration tests; do not migrate an active database.

## Contract (blocking)

- [x] X-1 Add `adminSalesMetrics(from, to, categories, productIds)` and types (`SalesMetrics`, `TopProduct`, `RevenueByDay`, `StatusCount`) to `specs/schema.graphql`. Document staff/superuser access; timezone-aware UTC instants and `[from, to)`; revenue/`ordersCount` by `paid_at`; status counts by `created_at`; gross line-subtotal revenue; exact filters with matching orders counted once; dispatch boundaries; `UNAUTHENTICATED`/`FORBIDDEN`/`INVALID` with `extensions.field`; maximum 366 days; and last-30-days default when both bounds are omitted.
- [x] X-3 Add `adminSalesCategories` (distinct stored `product_boardgame.category` values) to the SDL with staff/superuser access and documented `UNAUTHENTICATED`/`FORBIDDEN` authorization errors.

## Implementation

- [ ] C-1 Write failing tests in `tests/test_admin_sales_metrics_postgres.py`: `[from, to)` boundaries; only `paid` counts as revenue; subtotal-based revenue; exact category and product filters; order with ≥1 matching line counted once, revenue only from matching lines; top-10 order (units, revenue, bgg_id); `revenueByDay` in UTC; dispatch boundaries 23h59m/24h/71h59m/72h; rejected/cancelled/`paid_stock_conflict` excluded from dispatch counts; payment-status counts by `created_at`; empty range; invalid range, >366 days, `from >= to`; 401/403. (after X-1)
- [ ] C-2 Implement `adminSalesMetrics` resolver in `saleor/graphql/api.py` (or a new module imported there): staff/superuser guard, validation, parameterized SQL, `Decimal` throughout. Make C-1 pass.
- [ ] C-3 Implement `adminSalesCategories` resolver with test. (after X-3)
- [ ] C-4 Review `EXPLAIN` on seeded data; add an index migration only if justified, as a separate reviewed step. Record the outcome either way.
- [ ] C-5 Run Core tests for touched areas; record results in the global ledger.
