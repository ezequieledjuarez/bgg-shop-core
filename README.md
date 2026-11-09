# 🎲 BGG Shop - Core API (`bgg-shop-core`)

[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![Django 4.2](https://img.shields.io/badge/Django-4.2-green.svg)](https://www.djangoproject.com/)
[![GraphQL](https://img.shields.io/badge/GraphQL-Graphene-pink.svg)](https://graphene-python.org/)
[![Database](https://img.shields.io/badge/PostgreSQL-15-blue)](https://www.postgresql.org/)

## 📌 Descripción General

**`bgg-shop-core`** es el backend de dominio transaccional (OLTP) y fuente central de verdad para la plataforma e-commerce de juegos de mesa **BGG Shop**.

Construido con **Django** y **Graphene**, este servicio aplica reglas de
dominio, persiste datos en PostgreSQL y proporciona tareas asíncronas mediante
Celery con Redis como broker/cache. El BFF Flask es su consumidor REST-a-
GraphQL para el Frontend React/Vite; el navegador no llama directamente al
Core.

---

## 🏗️ Arquitectura y Rol en el Sistema

El flujo de solicitudes implementado es:

```text
Frontend React/Vite ── REST/JSON ──> BFF Flask ── GraphQL ──> Core Django/Graphene
                                                               │
                                        PostgreSQL ◄────────────┤
                                        Redis / Celery ◄────────┘
```

* **OLTP Transaccional:** Administra usuarios, catálogo de productos, variantes y stock con integridad ACID.
* **API de dominio:** Expone operaciones GraphQL fuertemente tipadas mediante Graphene.
* **Asincronismo:** Ejecuta tareas mediante Celery Worker/Beat con Redis.

El esquema GraphQL de referencia está en
[`specs/schema.graphql`](specs/schema.graphql). La referencia REST consumida por
el Frontend es [`../bgg-shop-bff/specs/openapi.yaml`](../bgg-shop-bff/specs/openapi.yaml).

## Servicio GraphQL expuesto

La ruta actual es `POST /graphql/`; GraphiQL está habilitado sobre la misma
ruta para exploración manual. En Compose está disponible en
`http://localhost:8000/graphql/`.

| Operación | Argumentos | Resultado o error |
| --- | --- | --- |
| `hello` | Ninguno | Texto `Saleor API activa y respondiendo OK`. |
| `products` | `page`, `pageSize`, `search`, `publisher`, `yearPublished`, `minPlayers`, `maxPlayers`, `categoryId`, `categoryName`, `priceMin`, `priceMax` | `ProductPage` filtrado con productos y metadatos de paginación. `publisher` aplica coincidencia exacta y sensible a mayúsculas luego de recortar espacios. |
| `productPublishers` | Ninguno | Lista completa y global de editoriales con conteos de juegos distintos del catálogo público. |
| `myWishlist` | None; requires an access token | Lists products saved by the authenticated buyer. |
| `addToWishlist` | `bggId`; requires an access token | Idempotently saves a product and returns its data. |
| `removeFromWishlist` | `bggId`; requires an access token | Removes a product from the buyer's Wishlist and returns its data. |
| `register` | `RegisterInput` (`email`, `password`, `firstName`, `lastName`) | Usuario creado y errores de dominio; no emite sesión. |
| `login` | `LoginInput` (`email`, `password`) | Usuario autenticado, JWT access/refresh y errores de dominio. |
| `adminSalesMetrics` | `from`, `to`, `categories`, `productIds`; requires staff/superuser | Totales de ventas brutas, productos principales, estados y serie diaria. |
| `adminSalesCategories` | Requires staff/superuser | Categorías almacenadas distintas para filtrar el panel de ventas. |

Ejemplo de consulta:

```bash
curl http://localhost:8000/graphql/ \
  -H 'Content-Type: application/json' \
  --data '{"query":"query { hello products(page: 1, pageSize: 20) { items { bggId name releaseYear minPlayers maxPlayers playingTimeMinutes minAge weight category price publisher imageUrl } page pageSize total totalPages } }"}'
```

`page` y `pageSize` deben ser enteros positivos; los rangos invertidos también
se rechazan. Los errores GraphQL se devuelven en el formato de GraphQL
(`errors`); la consulta de productos filtra `product_boardgame` antes de
contar y paginar. `categoryId` usa el mapa local `1` estrategia, `2` temática,
`3` familiar. El Core no expone REST para el catálogo: entrega el stock
(`quantity`) y las operaciones de catálogo/carrito, Wishlist, checkout y
pedidos mediante GraphQL. El carrito no reserva ni descuenta stock. La ruta
HTTP para descargar facturas se describe en la sección de compras. La
documentación completa de autenticación está en
[`docs/CU-C01-AUTHENTICATION-CHANGES.md`](../docs/CU-C01-AUTHENTICATION-CHANGES.md).

### Métricas de ventas (Admin)

`adminSalesMetrics` requiere un usuario activo staff o superuser. `from` y `to`
son límites ISO-8601 con zona horaria, se envían juntos o se omiten juntos, y
definen el intervalo semiabierto UTC `[from, to)`; si ambos se omiten, se usa
el rango de los últimos 30 días. Revenue y órdenes pagas usan `paid_at`; los
conteos de pago y despacho usan `created_at`. Revenue suma subtotales
históricos de líneas y los filtros de categoría/producto cuentan cada orden
coincidente una sola vez. `adminSalesCategories` devuelve valores distintos
almacenados en `product_boardgame.category` y aplica la misma autorización.
El contrato REST consumido por el panel está documentado en el
[`README del BFF`](../bgg-shop-bff/README.md).

### Esquema de producto y paginación

GraphQL presenta los campos en `camelCase`:

```graphql
type Product {
  bggId: Int!
  name: String!
  releaseYear: Int!
  minPlayers: Int!
  maxPlayers: Int!
  playingTimeMinutes: Int!
  minAge: Int!
  weight: Float!
  category: [String!]!
  price: Float!
  publisher: String!
  imageUrl: String!
}

type ProductPage {
  items: [Product!]!
  page: Int!
  pageSize: Int!
  total: Int!
  totalPages: Int!
}
```

El BFF adapta estos nombres a `snake_case` (`bgg_id`, `page_size`,
`total_pages`, etc.) antes de responder por REST.

`productPublishers` cuenta `bggId` distintos entre los mismos productos
elegibles para el catálogo público, sin depender de búsqueda, categoría ni
paginación. Agrupa nombres luego de recortar espacios, conserva variantes de
mayúsculas/minúsculas y excluye nombres vacíos. `publisher` usa ese nombre
recortado como identidad exacta y se combina con los demás filtros. El
Frontend consume el catálogo a través del BFF; no llama GraphQL directamente.
Para el detalle de CU-C02, ver
[`docs/CU-C02-FILTERING-CHANGES.md`](../docs/CU-C02-FILTERING-CHANGES.md).

### Persistencia de conversaciones del chatbot (CU-C07, en progreso)

El Core implementa las operaciones GraphQL `createChatConversation`,
`chatConversation` y `recordChatExchange`. Las conversaciones y mensajes se
guardan en PostgreSQL con propietario autenticado o anónimo validado; el
identificador de propietario no se acepta como argumento de cliente. La
migración es [`saleor/graphql/migrations/0020_chat_conversations.py`](saleor/graphql/migrations/0020_chat_conversations.py).

Esto cubre solo la base de persistencia, no el chatbot completo. No está
implementada la mutación `deleteChatConversation` declarada en el SDL, ni la
retención automática de 30 días. No hay recuperación por ChromaDB, conexión
con un modelo ni respuestas fundamentadas: el BFF todavía devuelve un
fallback determinista. ChromaDB sigue siendo un requisito aprobado para una
fase futura, no un componente instalado o ejecutado. El OpenAPI del BFF y el
SDL incluyen partes planificadas que aún no son funcionalidad disponible.

Trazabilidad: [`../specs/013-add-chatbot/spec.md`](../specs/013-add-chatbot/spec.md),
[`../specs/013-add-chatbot/plan.md`](../specs/013-add-chatbot/plan.md),
[`../odd/tasks/013-add-chatbot.md`](../odd/tasks/013-add-chatbot.md);
implementación en [`saleor/graphql/api.py`](saleor/graphql/api.py); pruebas
en [`tests/test_graphql_chat.py`](tests/test_graphql_chat.py). Las pruebas de
persistencia se verificaron de forma aislada según el plan, pero eso no
equivale a validar el servicio completo o su integración con el Frontend.

---

## 🛠️ Tech Stack

* **Lenguaje:** Python 3.12+
* **Framework Web:** Django 4.2+ (Saleor Architecture)
* **Capa de API:** Graphene-Django (GraphQL)
* **Base de Datos:** PostgreSQL 15
* **Broker & Cache:** Redis + Celery
* **Contenerización:** Podman / Podman Compose (Rootless)

---

## 🚀 Inicio Rápido (Entorno Local con Podman)

Se requiere Podman con proveedor Compose. Para levantar Core, PostgreSQL,
Redis, Celery y el resto de dependencias del proyecto, ejecuta desde la raíz
del repositorio:

```bash
podman compose -f podman-compose.yaml up -d --build
podman compose -f podman-compose.yaml exec api python manage.py migrate
```

GraphQL/GraphiQL queda publicado en `http://localhost:8000/graphql/`. El BFF
accede al Core por `http://api:8000/graphql/` dentro de la red Compose. Los
valores predeterminados del Compose son solo para desarrollo local. Para
detener los contenedores sin borrar volúmenes:

```bash
podman compose -f podman-compose.yaml down
```

## Purchase checkout

Authenticated GraphQL buyers can use `checkout`, `retryPayment`, and `confirmPayment`, then query `myOrders` or `myOrder`. Paid orders expose an invoice number and the PDF route `GET /invoices/<order_id>/`. Mercado Pago Checkout Pro is sandbox-only; inventory is reduced only after the verified payment is approved.

Configure `MP_ACCESS_TOKEN`, `MP_SANDBOX=true`, and either `MP_CURRENCY_ID` or `COUNTRY_MONEY_CODE` (AR defaults to ARS). Set `FRONTEND_URL` for payment return URLs. Keep the access token in the API environment only and never commit it. The order and invoice tables are provisioned by the additive DDL in `../import_catalog.sql`.
