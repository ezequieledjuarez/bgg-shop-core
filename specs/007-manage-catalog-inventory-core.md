# CU-A01 — Core Responsibilities (Saleor)

Reference: ../007-manage-catalog-inventory/spec.md

## Purpose
Definir y exponer la fuente de verdad del catálogo: modelos de producto/variante, stock, promociones y auditoría. Garantizar integridad transaccional y reglas de negocio (stock reservations, promotion evaluation).

## Responsibilities
- Mantener modelos persistentes y lógicos: Product, Variant (sku, attributes, stock), Promotion, Coupon, AuditLog.
- Proveer operaciones atómicas para: crear/editar/despublicar productos; ajustar stock con validación contra reservas; crear/editar promociones y cupones; gestionar contador global de usos de cupones.
- Implementar lógica de negocio: aplicación de promociones (`stackable` flag), validación de cupones (global limit), bloqueo de reducciones de stock por debajo de reservas.
- Registrar auditoría para todas las operaciones administrativas (usuario, acción, diffs, timestamp).
- Emitir eventos (por ejemplo via webhook o event bus) tras cambios críticos: product.updated, stock.changed, promotion.updated, coupon.used, audit.logged.
- Exponer API interna (GraphQL/REST) consumible por BFF con contratos estables y paginación/filtrado donde aplique.

## Data Contracts / Endpoints (suggested)
- GET /internal/products?updated_since=... → list products (full fidelity)
- POST /internal/products → create product (returns id)
- PUT /internal/products/{id} → update product
- POST /internal/products/{id}/variants/{vid}/adjust-stock → {delta:int} → validates reservations
- POST /internal/promotions → create/update promotions (includes `stackable` boolean)
- POST /internal/coupons/{code}/apply → validates and decrements global counter atomically
- GET /internal/audit?entity=product&id=... → audit records

Notes: Use transactional DB operations; coupon counter must be updated in a safe atomic way (DB row increment/compare or dedicated counter table).

## Acceptance Criteria (Core)
- AC-Core-1: Product and Variant models persist correctly and exposed endpoints return consistent data.
- AC-Core-2: Stock adjustments that would reduce below reserved units are rejected with an explicit error code and message.
- AC-Core-3: Promotion evaluation implements `stackable` semantics; flag present in stored promotion entities.
- AC-Core-4: Coupon application decrements the global counter atomically and rejects when exhausted.
- AC-Core-5: Audit records are created for every create/edit/delete action with user and timestamp.

## Tests (Core)
- Unit tests for model validation and business rules (stock, promotions, coupons).
- Integration tests exercising transactional behaviour for concurrent stock changes and concurrent coupon redemptions.
- Schema/e2e tests ensuring exposed endpoints match BFF contract.

## Notes & Dependencies
- Database migrations required for `stackable` and coupon counter fields.
- Ensure reservation mechanism exists (or integrate with order workflow) so reserved units can be computed.

## Next steps
- Provide exact API schema (request/response JSON) for BFF to implement.
- Create migration plan for core DB changes and tests for concurrency.
