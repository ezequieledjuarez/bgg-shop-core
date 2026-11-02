# 008 — Operations & Keys (Auth por Ops) — Especificación

La estructura retrospectiva para Spec Kit está en [008-operations-keys-auth/](008-operations-keys-auth/spec.md), con especificación, plan y tareas; este documento conserva las notas y decisiones originales.

Estado: cerrada y validada con salvedades el 2026-10-30, con aprobación del responsable. La cobertura histórica de Playwright no ejecutada fue aceptada como excepción y no se considera aprobada.

Fecha: 2026-10-25
Autor: equipo

Resumen
-------
Esta especificación describe la implementación de un mecanismo de autorización por "operations & keys".
La autoridad de persistencia será el servicio Django (`bgg-shop-core`) usando el ORM de Django. El BFF (`bgg-shop-bff`) validará operaciones enviando el `X-Op-Key` (header) y consultando el core (o endpoint interno) para verificar que la key existe, no está revocada, y contiene la operación solicitada.

Objetivos
---------
- Permitir control por-URL+method (op) por sesión/usuario.
- Emitir keys por login y entregarlas al cliente (single-view: el token se muestra SOLO en el momento de creación).
- Validación desde el BFF mediante una consulta interna al Core en cada solicitud.
- Soportar revocación, expiración y auditoría por session/key.
- Mantener compatibilidad con `Authorization` (JWT) durante la migración.

Alcance
-------
- Modelos y migraciones en `bgg-shop-core`.
- Endpoint interno de validación (`/internal/op-validate`) en Core (autorizado sólo para BFF y servicios internos).
- Cambios en `bgg-shop-bff`: extracción de `X-Op-Key`, middleware/decorator `op_key_required`, fallback a JWT `Authorization`.
- Cambios en frontend/E2E: guardar `op_key` en `sessionStorage` y usar `X-Op-Key` en llamadas (a través de `initFetchAuth.js`).

Terminología
-----------
- op: string que identifica una operación permitida para una sesión, por ejemplo `admin:products:POST` o `GET:/api/v1/admin/products`.
- key / op_key: token opaco generado en login, devuelto al cliente. En BD se almacena su hash.
- session: entidad que agrupa `user + ops + key + metadata`.

Diseño de datos (Django ORM)
---------------------------
Crear nueva app o módulo en `bgg-shop-core`, por ejemplo `opkeys`.

Modelo propuesto (simplificado):

```py
from django.db import models
from django.conf import settings
import uuid

class OpSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    key_hash = models.CharField(max_length=256, db_index=True)  # HMAC-SHA256 or bcrypt
    ops = models.JSONField(default=list)  # lista de strings "ops"
    issued_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used = models.DateTimeField(null=True, blank=True)
    revoked = models.BooleanField(default=False)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = 'op_session'

    def is_valid(self):
        from django.utils import timezone
        if self.revoked: return False
        if self.expires_at and self.expires_at < timezone.now(): return False
        return True
```

Notas:
- `key_hash` debe ser calculado con HMAC-SHA256(secret, raw_key) y almacenado; alternativamente usar bcrypt/argon2. Nunca almacenar `raw_key`.

Generación de key (recomendación):
- raw_key = base64.urlsafe_b64encode(os.urandom(32)).decode('ascii')
- store key_hash = HMAC(secret, raw_key) or bcrypt(key)
- devolver `raw_key` al cliente una sola vez.

API / Contractos
-----------------

1) Login (Core) — cambios mínimos
- Después de autenticación, generar `OpSession` si corresponde y devolver (en response body) un bloque opcional:

Response (ejemplo):

```json
{
  "session": { "access_token": "..." },
  "op_key": "<raw_key>",
  "op_key_expires_at": "2026-10-25T12:34:56Z",
  "ops": ["GET:/api/v1/admin/products","POST:/api/v1/admin/products"]
}
```

Nota: el `op_key` se muestra SÓLO en el momento del login; el cliente es responsable de almacenarlo (ej. `sessionStorage`).

2) Validación interna (Core) — endpoint privado (BFF -> Core)
- Path: POST `/internal/op-validate`
- Autorización: whitelist por IP o mutual-TLS o header interno; restringir a servicios de backend.
- Payload:

```json
{ "op_key": "<raw_key>", "path": "/api/v1/admin/products", "method": "POST" }
```

- Response 200 OK (válido):

```json
{ "valid": true, "session_id": "<uuid>", "user_id": 123, "ops": ["..."], "expires_at": "..." }
```

- Response 403 / 401 / 404 cuando no válido o no encontrado.

Alternativa: exponer GraphQL query en core para validar la key si se prefiere no añadir endpoint REST.

Middleware / BFF
----------------

Design (pseudocódigo Flask):

```py
from flask import request, g
def op_key_required(op_name):
    def decorator(f):
        def wrapper(*args, **kwargs):
            raw = request.headers.get('X-Op-Key')
            if not raw:
                # fallback a Authorization JWT (legacy)
                return check_jwt_or_fail()
            # call internal validation endpoint on core
            res = internal_validate_key(raw, request.path, request.method)
            if not res.valid: return jsonify({'error':'unauthorized'}), 401
            g.op_session = res  # contains session_id, user_id, ops
            return f(*args, **kwargs)
        return wrapper
    return decorator
```

Implementación práctica:
- `src/auth.py` añadir `op_key_required` y usarlo en controladores admin.
- El BFF consulta Core en cada solicitud con `X-Op-Key` y no almacena decisiones de autorización.

Mapping op: ruta vs op-name
--------------------------
- Opciones:
  - usar strings canónicas `METHOD:PATH` y comparar directamente con `ops` almacenadas
  - permitir expresiones con comodines `/api/v1/admin/products*`
- Recomendación: guardar ops normalizadas `GET:/api/v1/admin/products` y en middleware normalizar request to `METHOD:PATH` y hacer matching con startswith o glob.

Cache y rendimiento
-------------------
- BFF: no cachear validaciones; esto evita que una revocación quede demorada, a costa de una llamada a Core por solicitud.
- Core: agregar índices en `key_hash` y `user` para consultas rápidas.

Seguridad
---------
- La emisión es optativa: `OPKEYS_ISSUE_ON_LOGIN` está desactivado por defecto. El frontend usa JWT por defecto; `VITE_USE_OPKEY=true` habilita OpKey para operaciones administrativas incluidas en la sesión y la API configurada. Desactivar la emisión no revoca claves ya emitidas.
- Las rutas internas de creación, validación y revocación requieren `X-Internal-Secret`. Core y BFF deben recibir el mismo `OPKEY_INTERNAL_SECRET`, generado con un generador criptográfico y almacenado de forma segura. Debe tener al menos 32 bytes; si falta o es más corto, la validación falla de forma cerrada. El valor de ejemplo anterior ya no es válido.
- La respuesta que contiene una nueva clave incluye `Cache-Control: no-store`. El BFF consulta Core para cada solicitud con `X-Op-Key`, sin cachear decisiones ni conservar la clave en memoria más allá de la solicitud.
- La siguiente validación observa la revocación, aunque una solicitud ya autorizada puede continuar. Cada validación requiere acceso a Core; si falla o devuelve una respuesta inválida, el BFF deniega el uso de la clave y no sigue redirecciones.
- El BFF delega a GraphQL únicamente una OpKey validada para la ruta y método administrativos originales, acompañada del secreto interno; Core vuelve a comprobar la operación y el rol activo del usuario. No se utiliza un token administrativo compartido.
- Antes de ejecutar resolvers, Core valida el AST de la operación seleccionada
  para cada delegación OpKey administrativa. Solo permite una selección raíz
  correspondiente al método y ruta autorizados, y vincula los identificadores
  y acciones pertinentes. Los alias y fragmentos no pueden introducir una
  segunda selección. Solo se acepta JSON sin parámetros de consulta; otros
  transportes, lotes y rutas desconocidas fallan de forma cerrada. El resolver
  de stock verifica además el ID por separado. Si hay `Authorization`, la
  identidad utilizada sigue siendo JWT, incluso para WhoAmI.
- Las correspondencias probadas incluyen sesiones de operación, emisión de
  gift cards, creación y edición de productos, ingreso de stock y listado,
  creación y edición de promociones. El BFF intenta `productByBggId` y
  `applyCouponPreview`, pero el esquema Core actual no los expone; no se
  acredita que estas operaciones funcionen de extremo a extremo. La prueba
  positiva del listado de promociones usa una selección válida; la consulta
  real del BFF solicita `discountPercent` y `active`, ausentes en `Promotion`.
- Para una ruta administrativa con solo OpKey, una clave inválida responde
  401; una falla de transporte, configuración o respuesta inválida de Core
  responde 502. Con JWT presente se conserva su verificación independiente.
- Revisión local del 2026-10-31: Core aprobó 155 pruebas en SQLite en memoria
  y BFF aprobó 119, con una falla preexistente por la ruta antigua
  `/admin/products`. Se bloquearon sockets; no se probaron servicios ni
  concurrencia PostgreSQL.
- BFF revalida la identidad JWT contra Core en cada solicitud administrativa,
  sin cachear el resultado. Esto no implementa revocación de JWT: Core sigue
  aceptando tokens emitidos hasta su vencimiento, como se indica abajo.
- Verificación local del 2026-10-31: los archivos
  `tests/test_graphql_products.py tests/test_opkeys_internal.py` de Core dieron
  105 aprobadas con `python3 -m pytest -p no:cacheprovider ... -q` ejecutado
  mediante `pytest.main`, bloqueando sockets y la conexión de base por defecto.
  Se utilizaron mocks y SQLite en memoria, sin servicios ni migraciones.
- Compose limita la publicación del puerto Core a `127.0.0.1` en el host; el BFF conserva acceso por la red de contenedores. La configuración requiere recrear el servicio para aplicarse y no aísla Core de otros contenedores de la misma red.
- Transporte local: este TP se ejecuta exclusivamente para pruebas locales y no cuenta con un dominio HTTPS viable. La configuración actual usa HTTP, sin cifrado ni autenticación del transporte; no debe usarse en redes compartidas ni con credenciales reales. BFF y frontend se publican actualmente en todas las interfaces, por lo que el alcance local del TP no implica aislamiento de red.
- Hashing: preferir HMAC-SHA256(server_secret, raw_key) y almacenar el HMAC; al validar, re-hachear y comparar en tiempo constante.
- Rotación manual aceptada: un nuevo login puede emitir otra OpKey y un administrador puede revocar la sesión anterior por separado. No existe una operación de rotación atómica: la clave anterior sigue vigente hasta confirmar su revocación.
- Revocación: API administrativa para revocar `OpSession`.
- Auditoría: `opkeys_opkeyaudit` registra sesión, sujeto, actor, origen, acción y fecha sin almacenar claves ni hashes. Emisión y revocación se registran en la misma transacción que el cambio de sesión. Un fallo de auditoría impide el cambio o la validación; en login puede continuar la autenticación JWT sin emitir OpKey.
- El vencimiento se registra cuando se observa durante la validación, no mediante un proceso que detecte todas las expiraciones. Se registra una sola observación mientras exista el evento; tras la purga puede registrarse otra. El actor `KEY_HOLDER` se infiere del propietario de la sesión y no acredita la identidad de quien presentó la clave.
- Retención: `purge_opkey_audit` informa los eventos anteriores a siete días y `--apply` los elimina. El servicio Compose `opkey-audit-retention` del perfil `opkey-audit` ejecuta la purga cada minuto; requiere supervisión operativa y una interrupción puede retrasar la eliminación.

Compatibilidad y migración
-------------------------
- Mantener validación JWT por `Authorization` hasta migración completa.
- Rollout:
  1. Implementar modelos y endpoint interno en `bgg-shop-core` (sin usar en BFF aún).
    2. Implementar middleware en `bgg-shop-bff` con fallback a JWT. Activar en el ambiente local.
  3. Actualizar login y frontend para generar y devolver `op_key` en nuevas sesiones.
  4. Gradualmente habilitar `op_key` required en endpoints sensibles y retirar JWT si procede.

Pruebas y criterios de aceptación
--------------------------------
- Unit tests en `bgg-shop-core` para crear/validar/revocar `OpSession`.
- Integration test: BFF -> Core `/internal/op-validate` responder correctamente.
- E2E: Playwright tests que antes fallaban deben poder autenticarse utilizando `op_key` (guardar en `sessionStorage`) y ejecutar flows admin.
- Métrica de aceptación: las llamadas admin en E2E ya no reciben 401 por ausencia de Authorization y las pruebas pasan.
- La prueba local reproducible se ejecuta desde `bgg-shop-frontend` con `npm run test:e2e:opkey`. Requiere los servicios locales activos, el contenedor `saleor_api`, Podman, dependencias Node y Chromium de Playwright. Crea un administrador sintético temporal en la base activa, prueba acceso y logout, verifica la auditoría y elimina al usuario y sus sesiones; conserva los eventos de auditoría. No modifica productos ni realiza pagos.
- El comando ejecuta únicamente las pruebas de `admin.spec.ts`; no sustituye la validación del resto de la suite Playwright. Usa un directorio temporal para los resultados y no ejecuta el setup compartido que guarda sesiones en disco.

Salvedades de validación (2026-10-30)
------------------------------------
- Se ejecutaron 55 pruebas enfocadas de Core e integración y dos pruebas E2E locales con Playwright, incluido login, acceso administrativo con OpKey y logout. Además, cuatro pruebas Chromium pasaron en un entorno descartable, incluyendo las ramas aisladas de creación y edición administrativas con OpKey y sin JWT. No se ejecutaron las ramas antiguas ni el resto de la suite Playwright; no se encontró un inventario completo de las pruebas que fallaban.
- La revocación de JWT se difirió: el logout revoca la OpKey, pero los JWT emitidos siguen vigentes.
- HTTP no cifra las credenciales y BFF/frontend escuchan en todas las interfaces. El alcance es exclusivamente local y de pruebas; no usar esta configuración en redes compartidas ni con credenciales reales.
- Se aceptó la rotación manual no atómica: la clave anterior permanece vigente hasta confirmar su revocación.
- La retención de siete días depende del worker; su interrupción puede retrasar la purga. La expiración se audita cuando una validación la observa, no al ocurrir.
- Se detectó una incidencia independiente de catálogo: editar un producto creado sin año puede fallar porque el valor guardado `0` no pasa la validación del formulario. La prueba aislada usó un año válido.

Operaciones administrativas
-------------------------
- `GET /api/v1/admin/op-sessions` lista sesiones y operaciones sin exponer claves ni verificadores; acepta `user_id`, `page` y `page_size`.
- `POST /api/v1/admin/op-sessions/{session_id}/revoke` y `/expire` revocan o adelantan el vencimiento. Exigen un administrador activo mediante JWT u OpKey autorizada; Core registra el actor, la acción y una referencia derivada de la sesión.
- Estas operaciones están disponibles por REST y GraphQL; no hay una pantalla administrativa específica. La auditoría durable registra al administrador autenticado.

Esquema de migración (comandos)
------------------------------
1. Crear app `opkeys` en `bgg-shop-core` y añadir modelos.
2. Ejecutar:

```bash
cd bgg-shop-core
python manage.py migrate opkeys 0002
```

3. Antes de migrar una base existente, verificar la migración en una base aislada y realizar un respaldo. Desplegar core con endpoint interno habilitado y protegido por red.
4. Desplegar BFF con middleware en modo fallback inicialmente.

Configuración local activa
--------------------------
- El secreto compartido está fuera del repositorio, en `~/.config/boardgame-app/opkey-internal-secret`, con permisos `600`. No imprimirlo ni incorporarlo a archivos versionados.
- Para recrear los servicios conservando esta configuración, ejecutar desde la raíz del workspace:

```bash
OPKEY_INTERNAL_SECRET="$(< "$HOME/.config/boardgame-app/opkey-internal-secret")" \
OPKEYS_ISSUE_ON_LOGIN=true VITE_USE_OPKEY=true \
podman-compose --profile opkey-audit up -d --no-deps api bff frontend opkey-audit-retention
```

- Las flags y el secreto se pasan a Compose al recrear los contenedores; un reinicio sin recreación conserva su configuración. La revocación de JWT sigue pendiente.

Riesgos conocidos
-----------------
- Latencia extra si la validación se hace en cada request sin cache.
- Gestión de claves a largo plazo: si no se rotan, pueden permanecer válidas.
- Necesidad de coordinar despliegues entre Core y BFF para no romper autenticación.

Plan de trabajo resumido
-----------------------
1. Implementar modelos + migración en `bgg-shop-core`.
2. Añadir endpoint interno `/internal/op-validate` (autenticado por red/mTLS/header privado).
3. Implementar la validación de `X-Op-Key` en `bgg-shop-bff`, con consulta a Core y fallback JWT.
4. Actualizar login para generar y devolver `op_key` y `ops`.
5. Actualizar frontend/E2E para usar `X-Op-Key`.
6. Validar en el ambiente local e iterar.

Anexos / ejemplos
-----------------
- Ejemplo generación de raw key (Python):

```py
import base64, os
raw = base64.urlsafe_b64encode(os.urandom(32)).decode('ascii')
```

- Ejemplo HMAC hash (Python):

```py
import hmac, hashlib
def hmac_hash(secret: bytes, raw_key: str) -> str:
    return hmac.new(secret, raw_key.encode('utf-8'), hashlib.sha256).hexdigest()
```

Fin de la especificación.

Caducidad y cierre de sesión
----------------------------

- TTL por defecto: las `OpSession` generadas en login tendrán `expires_at = issued_at + 30 minutes`.
- Comportamiento en BFF: al validar una `op_key`, además de comprobar `revoked == False`, se debe comprobar `expires_at` y considerar inválida cualquier sesión cuyo `expires_at` esté en el pasado.
- Renovación/Sliding window: por ahora NO aplicar renovación automática. Si la aplicación requiere sesión extendida por uso continuo, esto debe definirse explícitamente en una futura iteración.
- Logout: el frontend solicita al BFF el cierre de sesión, con la OpKey cuando existe, y conserva la sesión si no puede confirmar el resultado. El BFF revoca la OpKey cuando corresponde y limpia la cookie de refresh. Los JWT ya emitidos no se revocan.
- Limpieza: `python manage.py purge_expired_opkeys` informa las sesiones expiradas hace más de 30 días sin eliminarlas. `--retention-days` cambia la retención y `--apply` habilita la eliminación; no está programado ni autorizado para la base activa. Las sesiones sin vencimiento no se eliminan por este comando.
- Auditoría: cada uso válido actualiza `last_used`. La revocación y la expiración observada generan eventos durables conforme a la sección de Seguridad; en logout el actor se infiere del propietario de la clave y no acredita su identidad humana.

Consideraciones operativas
---------------------------
- TTL corto reduce impacto en caso de key leak, pero aumenta la frecuencia de re-login; 30 minutos es el valor inicial acordado.
- Si se decide permitir renovación automática, implementarla explícitamente con un flag `sliding=True` y límites de extensión (max 24h).

