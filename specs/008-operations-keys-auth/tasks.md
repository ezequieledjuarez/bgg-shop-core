# Tasks: Operations and Keys (008)

**Input**: [spec.md](spec.md), [plan.md](plan.md), and [original notes](./notes.md).
**Status**: Closed and validated with exceptions on 2026-10-30, with owner approval. A checked task does not imply that every related test has run.

## Implementation and validation

- [x] T001 [US1] Persist operation sessions and protect internal create, validate, and revoke endpoints with a shared secret. See [Core views](../../opkeys/views.py), [models](../../opkeys/models.py), and [internal tests](../../tests/test_opkeys_internal.py).
- [x] T002 [US1] Implement optional login issuance, BFF validation without authorization caching, and frontend OpKey use. See [Core GraphQL](../../saleor/graphql/api.py), [BFF middleware](../../../bgg-shop-bff/src/opkey_middleware.py), and [frontend session handling](../../../bgg-shop-frontend/src/auth/session.ts).
- [x] T003 [US2] Implement logout revocation and administrative session management. Manual, non-atomic rotation was accepted; [Core audit tests](../../tests/test_opkey_audit.py) cover revocation of the old session while the replacement remains valid.
- [x] T004 [US3] Add durable audit and seven-day retention; migration, rollback, concurrency, and retention were tested in isolated PostgreSQL before applying `opkeys.0002` to the backed-up local database.
- [x] T005 [US1] Record focused verification: 55 Core/integration tests and two local Chromium E2E tests passed on 2026-10-30. The E2E command and its database effects are documented in [original notes](./notes.md).
- [x] T006 [US1] Verify the identified historical Playwright scenarios in an isolated environment and obtain explicit acceptance of the remaining unverified coverage. Do not execute tests that expose credentials or mutate the active catalog.
- T006 evidence (2026-10-30): four Chromium tests passed against a disposable PostgreSQL/Core/BFF/frontend stack, including the previously identified product creation and editing scenarios with `X-Op-Key` and no JWT. Their isolated branches ran; their legacy branches and the rest of the Playwright suite did not. No exhaustive inventory of previously failing tests was found.
- [x] T007 Record owner approval on 2026-10-30 to close and validate the feature with the remaining historical Playwright coverage unverified. Unexecuted tests are not reported as passed.

## Deferred and accepted limitations

- JWT revocation is deferred; logout does not revoke previously issued JWTs.
- The project is local-only. Current HTTP traffic is unencrypted and BFF/frontend listen on all interfaces; the configuration is not suitable for shared networks or real credentials.
- Manual rotation is not atomic. Audit expiry events are recorded when observed, key-holder identity is inferred, and worker outages can delay retention.
- A separate catalog issue was observed: a product created without a year is stored with year `0`, which the edit form rejects. The isolated edit test used a valid year; this issue is outside OpKey authorization and remains unresolved.

## Dependencies

T001 and T004 support Core session and audit behavior. T002 depends on session validation; T003 depends on session management and audit. T005 records observed checks, while T006 and T007 remain independent from the checked implementation status.