# 008 — Operations & Keys (Auth por Ops) — Especificación

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
- Validación rápida desde el BFF mediante una consulta interna al Core (o caché local con TTL).
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
- `internal_validate_key` debe cachear respuestas por hash(key) TTL (ej. 2 min) para reducir latencia.

Mapping op: ruta vs op-name
--------------------------
- Opciones:
  - usar strings canónicas `METHOD:PATH` y comparar directamente con `ops` almacenadas
  - permitir expresiones con comodines `/api/v1/admin/products*`
- Recomendación: guardar ops normalizadas `GET:/api/v1/admin/products` y en middleware normalizar request to `METHOD:PATH` y hacer matching con startswith o glob.

Cache y rendimiento
-------------------
- BFF: mantener cache en memoria LRU por `key_hash` con TTL configurable (ej. 120s) para evitar llamadas al core por cada request.
- Core: agregar índices en `key_hash` y `user` para consultas rápidas.

Seguridad
---------
- Key raw debe ser enviado sólo sobre TLS.
- Hashing: preferir HMAC-SHA256(server_secret, raw_key) y almacenar el HMAC; al validar, re-hachear y comparar en tiempo constante.
- Rotación: poder generar nuevo raw_key y marcar anterior revoked.
- Revocación: API administrativa para revocar `OpSession`.
- Auditoría: registrar `last_used`, `ip`, y `x-debug-correlation` en logs.

Compatibilidad y migración
-------------------------
- Mantener validación JWT por `Authorization` hasta migración completa.
- Rollout:
  1. Implementar modelos y endpoint interno en `bgg-shop-core` (sin usar en BFF aún).
  2. Implementar middleware en `bgg-shop-bff` con fallback a JWT. Activar en staging.
  3. Actualizar login y frontend para generar y devolver `op_key` en nuevas sesiones.
  4. Gradualmente habilitar `op_key` required en endpoints sensibles y retirar JWT si procede.

Pruebas y criterios de aceptación
--------------------------------
- Unit tests en `bgg-shop-core` para crear/validar/revocar `OpSession`.
- Integration test: BFF -> Core `/internal/op-validate` responder correctamente.
- E2E: Playwright tests que antes fallaban deben poder autenticarse utilizando `op_key` (guardar en `sessionStorage`) y ejecutar flows admin.
- Métrica de aceptación: las llamadas admin en E2E ya no reciben 401 por ausencia de Authorization y las pruebas pasan.

Operaciones administrativas
-------------------------
- Endpoints admin para listar sesiones, revocar, forzar expiración, y visualizar ops por usuario.

Esquema de migración (comandos)
------------------------------
1. Crear app `opkeys` en `bgg-shop-core` y añadir modelos.
2. Ejecutar:

```bash
cd bgg-shop-core
python manage.py makemigrations opkeys
python manage.py migrate
```

3. Desplegar core con endpoint interno habilitado y protegido por red.
4. Desplegar BFF con middleware en modo fallback inicialmente.

Riesgos conocidos
-----------------
- Latencia extra si la validación se hace en cada request sin cache.
- Gestión de claves a largo plazo: si no se rotan, pueden permanecer válidas.
- Necesidad de coordinar despliegues entre Core y BFF para no romper autenticación.

Plan de trabajo resumido
-----------------------
1. Implementar modelos + migración en `bgg-shop-core`.
2. Añadir endpoint interno `/internal/op-validate` (autenticado por red/mTLS/header privado).
3. Implementar `op_key_required` en `bgg-shop-bff` con cache local y fallback JWT.
4. Actualizar login para generar y devolver `op_key` y `ops`.
5. Actualizar frontend/E2E para usar `X-Op-Key`.
6. Testear en staging, iterar.

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
- Logout: cuando el usuario cierre sesión (endpoint de logout en Core/BFF), el flujo debe:
    1. Buscar la `OpSession` asociada al `op_key` o a la sesión del usuario.
 2. Marcar `revoked = True` y actualizar `last_used` y `revoked_at` opcionalmente.
 3. Responder 204 y opcionalmente emitir un evento de auditoría.
- Limpieza: implementar un comando/cron job en Core que elimine (o archive) sesiones expiradas y/o revocadas (p. ej. `manage.py opkeys_purge_expired --days=30`).
- Logs/auditoría: registrar `last_used` cada vez que una `OpSession` se use con éxito y registrar el motivo (logout, expiry, revoke_manual) en el log o tabla de auditoría.

Consideraciones operativas
---------------------------
- TTL corto reduce impacto en caso de key leak, pero aumenta la frecuencia de re-login; 30 minutos es el valor inicial acordado.
- Si se decide permitir renovación automática, implementarla explícitamente con un flag `sliding=True` y límites de extensión (max 24h).

