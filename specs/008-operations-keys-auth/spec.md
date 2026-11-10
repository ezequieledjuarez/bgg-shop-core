# Feature Specification: Operations and Keys (008)

**Status**: Closed and validated with exceptions on 2026-10-30, with owner approval.
**Source**: [Original specification and operational notes](./notes.md)

## Objective and scope

Provide optional operation-scoped keys for administrative requests across Core, BFF, and frontend while preserving JWT compatibility. Core owns sessions, validation, revocation, expiration, and durable audit. BFF validates each presented key against Core, and the frontend can use a key for allowed administrative operations.

## User scenarios and acceptance

### US1: Issue and use an operation key

- When issuance is enabled and an active administrator logs in, the login can return an operation key, expiration, and allowed operations. The frontend stores the session in `sessionStorage` and sends `X-Op-Key` for eligible requests when enabled.
- BFF checks the key and requested method and path with Core on each request. Invalid, revoked, expired, unauthorized, or unverifiable keys do not authorize the request. JWT remains an alternative when applicable.

### US2: Revoke, expire, and rotate sessions

- Logout with an operation key requests its revocation; the next validation rejects it. An authenticated active administrator can list sessions and revoke or expire one.
- Manual rotation is accepted: a new login can issue a replacement and an administrator revokes the prior session separately. Both keys may remain valid until revocation is confirmed.

### US3: Audit security events

- Issuance and revocation produce durable audit records with the session, subject, actor, actor source, action, and timestamp, without raw keys or key hashes. Session changes and their audit records are atomic.
- Expiration is recorded when observed during validation. The actor associated with key-holder actions is inferred from the session owner, not proof of the human presenter.
- A retention worker removes audit events older than seven days; outages can delay removal. Its configuration and the dry-run cleanup command are described in the original specification.

## Functional requirements

- **FR-001**: Core stores operation sessions with a keyed hash, allowed `METHOD:PATH` operations, expiry, and revocation state; raw keys are returned only on issuance.
- **FR-002**: Internal create, validate, and revoke endpoints require the shared internal secret and fail closed when it is missing or invalid.
- **FR-003**: BFF validates every presented operation key with Core, applies the requested method and path, and does not cache authorization decisions.
- **FR-004**: Protected administrative operations require an active administrator. BFF delegates the validated key and original operation to Core for GraphQL authorization.
- **FR-005**: Login issuance and frontend key usage remain independently configurable; JWT compatibility remains available. Disabling issuance does not revoke existing keys.
- **FR-006**: Logout and administrative revocation take effect for the next validation; JWT revocation is deferred.
- **FR-007**: Audit issuance, revocation, administrative expiration, and observed expiration without storing credentials. Audit failures prevent the corresponding session change or expired-key validation; login may still succeed with JWT without issuing a key.
- **FR-008**: Retain audit records for seven days using the local worker. Actual purge can be delayed while the worker is unavailable.
- **FR-009**: Permit the accepted manual, non-atomic rotation procedure; do not infer revocation of the prior key merely from issuing a new one.
- **FR-010**: Limit this project to local testing. The current HTTP transport is unencrypted and BFF/frontend listen on all interfaces; do not use it on shared networks or with real credentials.

## Verification and limitations

- Focused Core and integration checks: 55 passed on 2026-10-30. The local `npm run test:e2e:opkey` run passed two Chromium tests, including real login, administrative access with OpKey, logout, and rejection after revocation.
- Four additional Chromium tests passed against a disposable local stack, including the isolated branches of the historical product creation and editing tests with OpKey and without JWT. The legacy branches and the remainder of the Playwright suite were not run; some tests alter catalog data or log sessions. An exhaustive inventory of previously failing tests was not found, so the original criterion is not fully verified.
- JWT revocation is deferred. HTTP transport and network exposure remain documented risks. Rotation is manual, and expiry is audited only when observed.
- A separate catalog issue remains: editing a product created without a year can fail because its stored year `0` is rejected by the edit form. The isolated edit test used a valid year.
- The owner explicitly accepted closing with the remaining historical Playwright coverage unverified on 2026-10-30. This is not a claim that the unexecuted tests passed.