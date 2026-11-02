# CU-A01 — Core Responsibilities (Saleor)

Reference: ../007-manage-catalog-inventory/spec.md

## Purpose
Definir y exponer la fuente de verdad del catálogo: productos, stock por producto, promociones y auditoría. Cada edición o variante de un juego es un producto independiente.

## Responsibilities
- Cuando dos clientes intenten pagar al mismo tiempo y quede un solo uso del cupón, el segundo espera a que termine el primero y recibe un aviso si el cupón se agota. Un pago confirmado como fallido no consume un uso. Si el resultado del primer pago es incierto, el último uso permanece bloqueado hasta confirmar el resultado. Un administrador consulta el pago y, si no obtiene respuesta, no se desbloquea el cupón; la falta de respuesta no confirma un fallo. La propuesta de liberarlo tras más de tres reintentos queda descartada. Los mecanismos de espera, consulta y confirmación siguen pendientes; esta regla no acredita implementación.
- Estas decisiones son requisitos, no evidencia de implementación: si una operación `IN`/`OUT` no impacta la base, se cancela o rechaza; una transacción efectivamente rechazada o revertida no deja el cambio aplicado. Si se confirma el rechazo o rollback sin cambios en la base, se permite procesar nuevamente un reintento con el mismo ID y datos. Un error de comunicación o HTTP no demuestra rollback: el servidor puede confirmar y perderse la respuesta, y un reintento aditivo podría duplicar la operación actual. Informar al usuario y registrar el error de forma segura sin detalles sensibles, sin suponer seguro reintentar con el contrato actual. Cada movimiento `IN`/`OUT` tendrá un identificador único; repetir el mismo ID y datos no debe duplicar el cambio, y reutilizarlo con datos diferentes debe rechazar la operación sin aplicar un nuevo cambio de stock. El ID puede registrarse provisionalmente como IN_PROGRESS; el cambio de stock, la auditoría y el resultado definitivo deben confirmarse atómicamente. Los ID de movimientos exitosos se conservarán indefinidamente, sin vencimiento, también ante reintentos tardíos. Fuera de la posibilidad de reintento indicada, el plazo y tratamiento de ID de fallos o rechazos no están aprobados. Quedan abiertos formato y alcance del ID, campos que determinan la identidad de los datos, detalles de la respuesta de rechazo, respuesta a reintentos de una operación aún en curso y recuperación del resultado incierto; definir y validar primero el contrato. Un cupón no se da de baja antes de que se realice el pago; no se presupone que todos sean de un solo uso y el mecanismo de espera y los resultados de pago inciertos siguen abiertos. Para `OUT` superior al stock no se establece confirmación ni código HTTP.
- La generación semanal del ZIP y el mecanismo que hace cumplir el permiso de borrado están pendientes y fuera del alcance actual de la 007; la solución futura no está implementada ni validada. La eliminación manual queda a cargo del usuario y está acordada solo para `is_superuser=true`; `is_staff` por sí solo no basta. No se afirma que el permiso se aplique en el filesystem.
- Mantener modelos persistentes y lógicos: Product con su stock, Promotion, Coupon y AuditLog.
- Proveer operaciones para crear/editar productos, ajustar stock y gestionar promociones y cupones. No existe despublicación y no se infiere eliminación física.
- Los cupones de descuento aplican a toda la compra; los cupones por producto no se implementan en este MVP. Una promoción de tienda elegible y específica para un producto prevalece sobre cualquier promoción global, incluso con porcentaje menor; si no existe, aplica la promoción global elegible de mayor porcentaje. No se combinan promociones y las filas de promoción enlazadas a cupones se excluyen de la selección de ofertas de tienda. La comparación cupón/tienda y el redondeo siguen el plan y los contratos generales. El camino de checkout está cubierto por pruebas de cálculo y por los casos PostgreSQL seleccionados `test_checkout_store_promotions_select_specific_and_highest_global_but_exclude_coupon_rows` y `test_checkout_total_uses_specific_and_highest_global_store_promotions` en `tests/test_graphql_credit_postgres.py`; ambos pasaron localmente, sin ejecutar el archivo completo. Gift cards y crédito son medios de pago, no cupones de descuento.
- Registrar auditoría para todas las operaciones administrativas (usuario, acción, diffs, timestamp). Para cambios de productos, movimientos de stock, promociones y cupones, incluir usuario, fecha, acción y valores anteriores y nuevos. La existencia de AuditLog no acredita su uso en todos los flujos ni su conformidad.
- La auditoría básica de esas cuatro áreas permanece en alcance; los registros, filtros, paginación y redacción tienen verificación local en las pruebas relacionadas y el flujo de auditoría del navegador. La retención futura (siete días en la base y ZIP local semanal) queda fuera de Spec 007: no se eliminarán filas sin verificar el archivo y no se acuerda purga automática. Siguen pendientes compatibilizar el plazo exacto de siete días con la ejecución semanal, el inicio/horario, el archivo atómico ante fallas, el acceso/consulta y el cifrado. Los ZIP se conservarán hasta eliminación manual, sin expiración automática; la decisión anterior de un mes queda reemplazada. Captura de secretos requiere revisión separada.
- Emitir eventos (por ejemplo via webhook o event bus) tras cambios críticos: product.updated, stock.changed, promotion.updated, coupon.used, audit.logged.
- Exponer API interna (GraphQL/REST) consumible por BFF con contratos estables y paginación/filtrado donde aplique.

## Data Contracts / Endpoints (suggested)
`specs/schema.graphql` describes the exposed GraphQL schema, including the
administrative `adminStockMovementStatus` lookup. It reads stored status by
UUID and rejects unauthorized requests. `adjustProductStock` accepts the UUID,
product ID, direction and positive quantity. It commits an `IN_PROGRESS` record
before processing, then commits stock, audit and the final `APPLIED` result
together. Replays with identical data return the original result; changed data
is rejected. A confirmed rejection may be retried with the same data. `OUT`
is limited to available stock and BFF reports the limitation. The legacy
`addProductStock` mutation rejects writes with `MIGRATION_REQUIRED`.

Migrations `0012_stock_movement_status` and `0013_stock_movement_details`
define the storage. They have not been applied to the active database. A
provisional record may remain `IN_PROGRESS` after an interruption; automatic
recovery of that situation is not implemented. A missing record does not prove
that a movement was rejected or rolled back. The BFF contract is in
`../bgg-shop-bff/specs/openapi.yaml`.

Local tests use mocks, SQLite and a disposable PostgreSQL 15 instance. Django's
migration executor applied 0012 and 0013 in an isolated schema; the preceding
migration chain was not tested. PostgreSQL tests cover concurrent movements,
replay, conflicting IDs, audit rollback, an in-progress record and a lost
response. Cross-service tests use an in-process transport and simulated
credentials; they do not establish a deployed end-to-end integration.

For a validated OpKey delegated from an administrative route, Core validates
the selected GraphQL operation before executing any resolver. It permits one
root selection matching that route and binds the relevant ID or action to the
path. For stock `adjustProductStock` must match the product ID; the resolver
also checks the ID independently. The legacy mutation cannot write stock.
Aliases and fragments are allowed
only when they expand to one root selection. JSON POST is the only supported
delegation transport; query-string overrides, batches and unknown routes fail
closed. Requests with an Authorization header continue to use JWT identity.
The prior schema concern is resolved: the BFF's product detail query is
delegated only for the exact
`GET /api/v1/admin/products/{bggId}` → `productByBggId(bggId)` binding, and
its promotions selection uses the Core `discountPct` field with a BFF-side
`discountPercent` alias. `bgg-shop-core/tests/test_opkeys_internal.py` verifies
valid and mismatched route, ID, method and GraphQL-root bindings. Final local
browser and checkout evidence is recorded in `odd/tasks/007-closeout.md`; it
does not constitute CI or deployment validation.

Local security recheck on 2026-10-31: Core's two focused test files passed
155 tests with SQLite in memory and socket connections blocked. Tests cover
the observed route mappings, cross-route rejection, ID/action bindings,
selected operations, variables, fragments, aliases, JWT identity and rejection
before stock resolvers execute. That historical check did not cover PostgreSQL
concurrency; later disposable PostgreSQL tests are described above.

The initial RED run reported 11 failed and 9 passed; its GREEN rerun reported
20 passed. The final full run reported 155 passed. Exact commands:

```sh
cd /home/eze/develop/boardgame-app/bgg-shop-core && DATABASE_URL=sqlite:///:memory: PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -c 'import socket, pytest; socket.socket.connect = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("network disabled")); socket.socket.connect_ex = socket.socket.connect; socket.create_connection = socket.socket.connect; raise SystemExit(pytest.main(["-p", "no:cacheprovider", "tests/test_opkeys_internal.py", "-q", "-k", "delegation_binds_route_operation_and_arguments or stock_aliases_rejected", "--tb=short"]))'
cd /home/eze/develop/boardgame-app/bgg-shop-core && DATABASE_URL=sqlite:///:memory: PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -c 'import socket, pytest; socket.socket.connect = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("network disabled")); socket.socket.connect_ex = socket.socket.connect; socket.create_connection = socket.socket.connect; raise SystemExit(pytest.main(["-p", "no:cacheprovider", "tests/test_graphql_products.py", "tests/test_opkeys_internal.py", "-q", "--tb=short"]))'
```

Local verification on 2026-10-31: `tests/test_graphql_products.py` and
`tests/test_opkeys_internal.py` passed 105 tests through `pytest.main` with
`-p no:cacheprovider -q --tb=short`, `PYTHONDONTWRITEBYTECODE=1` and
`PYTHONPATH=.`. Socket connections and Django's default database connection
were blocked in the test process; isolated in-memory SQLite and mocks were
used. The initial focused RED run had 8 failures and 4 passes; the final
focused stock-delegation run had 31 passes and 37 deselections. No live
database, migrations, network, or PostgreSQL concurrency test was used.

From the Core directory, the full test command was:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. DJANGO_SETTINGS_MODULE=saleor.settings python3 -c 'import socket, django, pytest; socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network disabled")); django.setup(); from django.db import connections; connections["default"].ensure_connection = lambda: (_ for _ in ()).throw(RuntimeError("default database disabled")); raise SystemExit(pytest.main(["-p", "no:cacheprovider", "tests/test_graphql_products.py", "tests/test_opkeys_internal.py", "-q", "--tb=short"]))'
```

- GET /internal/products?updated_since=... → list products (full fidelity)
- POST /internal/products → create product (returns id)
- PUT /internal/products/{id} → update product
- GraphQL: `adjustProductStock(productId: ID!, movementId: ID!, direction: StockMovementDirection!, quantity: Int!): AdjustProductStockPayload!`
- POST /internal/promotions → suggested create/update route; current selection rules are the approved product-specific-first, otherwise-highest-global behavior with no stacking and coupon-row exclusion.
- POST /internal/coupons/{code}/apply → suggested coupon validation route; checkout reservation/reconciliation behavior is defined in the current contracts and plan.
- GET /internal/audit?entity=product&id=... → audit records

Notes: Use transactional DB operations; coupon counter must be updated in a safe atomic way (DB row increment/compare or dedicated counter table).

## Acceptance Criteria (Core)
- AC-Core-1: Products and their stock persist correctly and exposed endpoints return consistent data.
- AC-Core-2: `IN` increases stock and `OUT` reduces it, including partially; at zero the product remains visible. Reservations are not made; when `OUT` exceeds available stock, inform the user that stock will reach zero.
- AC-Core-3: An eligible product-specific store promotion wins over global offers for that product; otherwise the highest eligible global offer applies. Store promotions do not stack and coupon-linked rows are excluded. Coupon/store comparison and rounding follow the overall plan/contracts.
- AC-Core-4: Coupons apply at payment; the global coupon counter is updated atomically and exhausted coupons are rejected. A confirmed failed payment does not consume a coupon use; uncertain outcomes remain reserved through reconciliation and unresolved cases require manual review.
- AC-Core-5: Audit records are created for every create/edit/delete action with user and timestamp. Product changes, stock movements, promotions, and coupons include user, date, action, and previous and new values. This existing audit requirement does not establish or require physical product deletion.

## Tests (Core)
- Unit tests for model validation and business rules (stock, promotions, coupons).
- Integration tests exercising transactional behaviour for concurrent stock changes and concurrent coupon redemptions.
- Schema/e2e tests ensuring exposed endpoints match BFF contract.

## Notes & Dependencies
- Existing persistence and migrations were reconciled and locally tested; see the task ledger and closeout for the tested scope.
- One `.webp` image per product is referenced by URL or filename; binary upload is not assumed.
- Implementation note: Core retains the legacy `stackable` field and reservation model/query; current checkout selection does not stack store offers. Product image constraints are represented by the contract and frontend flow, not by the promotion persistence model. Selected PostgreSQL tests verify promotion selection and the persisted checkout/provider total; the full PostgreSQL test file was not run. Weekly audit ZIP generation and related retention are outside Spec 007 and remain separate follow-up work.

## Status
Core contract, implementation, focused integration and local browser evidence
for Spec 007 are recorded in `specs/007-manage-catalog-inventory/tasks.md` and
`odd/tasks/007-closeout.md`. No CI, deployment or remote validation is claimed.
