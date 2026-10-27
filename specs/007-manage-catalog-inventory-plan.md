# Plan: CU-A01 — Core Implementation

Owner: bgg-shop-core
Reference spec: ../../specs/007-manage-catalog-inventory/spec.md

## Objective
Implement data model, business rules and safe operations that make Core the authoritative source for products, stock, promotions and coupons.

## Steps
1. Finish API contract with BFF (X-1).
2. Create DB migrations:
   - Add `promotions.stackable` boolean
   - Add `coupons.global_limit` and `coupons.used_count` (or counter table)
   - Ensure `audit_logs` table exists
3. Implement ORM models and validators.
4. Implement stock/reservation checks and `adjust-stock` endpoint.
5. Implement promotion evaluation service honoring `stackable`.
6. Implement coupon apply endpoint with atomic decrement.
7. Publish events on changes for BFF invalidation.
8. Add unit & integration tests including concurrency.
9. Prepare migration runbook and staging verification.

## Acceptance Criteria (Core)
- AC-Core-1..AC-Core-5 from core spec are met.

## Estimate
- Total: 6–12 working days

## Notes
- Use DB transactions and row-level locks where needed for counters.
- Coordinate contract & error codes with BFF before implementing endpoints.
