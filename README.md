# 🎲 BGG Shop - Core API (`bgg-shop-core`)

[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![Django 4.2](https://img.shields.io/badge/Django-4.2-green.svg)](https://www.djangoproject.com/)
[![GraphQL](https://img.shields.io/badge/GraphQL-Graphene-pink.svg)](https://graphene-python.org/)
[![Database](https://img.shields.io/badge/PostgreSQL-15-blue)](https://www.postgresql.org/)

## 📌 Descripción General

**`bgg-shop-core`** es el backend de dominio transaccional (OLTP) y fuente central de verdad para la plataforma e-commerce de juegos de mesa **BGG Shop**. 

Construido sobre **Django** y la arquitectura base de **Saleor Core**, este servicio abstrae la persitencia de datos en PostgreSQL, orquesta tareas asíncronas con Celery/Redis y expone las reglas de negocio a través de una API GraphQL unificada mediante **Graphene**.

---

## 🏗️ Arquitectura y Rol en el Sistema

Este repositorio opera de forma desacoplada dentro de la infraestructura
general. El flujo implementado es:
[ bgg-shop-front ]  ──(REST)──>  [ bgg-shop-bff ]  ──(GraphQL)──>  [ bgg-shop-core ]
(React / Vite)                  (Flask / Gateway)                 (Django / Saleor)
│
┌─────────┴─────────┐
▼                   ▼
[PostgreSQL]      [Redis / Celery]

* **OLTP Transaccional:** Administra usuarios, catálogo de productos, variantes y stock con integridad ACID.
* **Capa de Transporte Stricta:** Proporciona un esquema de GraphQL fuertemente tipado, previniendo problemas de *over-fetching* y *under-fetching*.
* **Asincronismo:** Procesa eventos en diferido (notificaciones, reportes masivos) mediante Celery Workers.

## Servicio GraphQL expuesto

La ruta actual es `POST /graphql/`; GraphiQL está habilitado sobre la misma
ruta para exploración manual. En Compose está disponible en
`http://localhost:8000/graphql/`.

| Operación | Argumentos | Resultado o error |
| --- | --- | --- |
| `hello` | Ninguno | Texto `Saleor API activa y respondiendo OK`. |
| `products` | `page`, `pageSize`, `search`, `yearPublished`, `minPlayers`, `maxPlayers`, `categoryId`, `priceMin`, `priceMax` | `ProductPage` filtrado con productos y metadatos de paginación. |
| `myWishlist` | None; requires an access token | Lists products saved by the authenticated buyer. |
| `addToWishlist` | `bggId`; requires an access token | Idempotently saves a product and returns its data. |
| `removeFromWishlist` | `bggId`; requires an access token | Removes a product from the buyer's Wishlist and returns its data. |
| `register` | `RegisterInput` (`email`, `password`, `firstName`, `lastName`) | Usuario creado y errores de dominio; no emite sesión. |
| `login` | `LoginInput` (`email`, `password`) | Usuario autenticado, JWT access/refresh y errores de dominio. |

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
`3` familiar. El Core no expone aquí endpoints REST de productos. Expone el stock (`quantity`)
y, por GraphQL, `cart`, `addToCart`, `updateCartQuantity` y `removeFromCart`
(el carrito no reserva ni descuenta stock). No expone checkout ni pedidos. La documentación completa de
autenticación está en
[`docs/CU-C01-AUTHENTICATION-CHANGES.md`](../docs/CU-C01-AUTHENTICATION-CHANGES.md).

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

El Frontend consume el catálogo a través del BFF; no llama GraphQL
directamente. Para el detalle de CU-C02, ver
[`docs/CU-C02-FILTERING-CHANGES.md`](../docs/CU-C02-FILTERING-CHANGES.md).

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

### Prerrequisitos
* Podman y Podman Compose instalados.
* Contenedores `db` y `redis` activos en la red compartida.

### 1. Clonar e Inicializar
```bash
git clone [https://github.com/tu-usuario/bgg-shop-core.git](https://github.com/tu-usuario/bgg-shop-core.git)
cd bgg-shop-core

## Purchase checkout

Authenticated GraphQL buyers can use `checkout`, `retryPayment`, and `confirmPayment`, then query `myOrders` or `myOrder`. Paid orders expose an invoice number and the PDF route `GET /invoices/<order_id>/`. Mercado Pago Checkout Pro is sandbox-only; inventory is reduced only after the verified payment is approved.

Configure `MP_ACCESS_TOKEN`, `MP_SANDBOX=true`, and either `MP_CURRENCY_ID` or `COUNTRY_MONEY_CODE` (AR defaults to ARS). Set `FRONTEND_URL` for payment return URLs. Keep the access token in the API environment only and never commit it. The order and invoice tables are provisioned by the additive DDL in `../import_catalog.sql`.
