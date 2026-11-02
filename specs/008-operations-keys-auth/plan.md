# Implementation Plan: Operations and Keys (008)

**Status**: Retrospective/as-built; closed and validated with exceptions on 2026-10-30. See [tasks.md](tasks.md).
**Specification**: [spec.md](spec.md)
**Original notes**: [008-operations-keys-auth.md](../008-operations-keys-auth.md)

## Summary

Core owns operation sessions and their durable audit. The Flask BFF consults Core for each operation-key request; the React frontend optionally sends the key for eligible administrative operations. Compatibility with JWT is retained, and JWT revocation is outside this iteration.

## Technical context

- Core: Django, Graphene, PostgreSQL, `opkeys` models and internal endpoints. Core GraphQL contract: [schema.graphql](../schema.graphql).
- BFF: Flask; REST contract: [openapi.yaml](../../../bgg-shop-bff/specs/openapi.yaml).
- Frontend: React, Vite, and Playwright; session storage and administrative requests are implemented in the frontend repository.
- Local deployment: Podman Compose. Core listens on the host loopback interface; BFF/frontend currently listen on all interfaces. HTTP does not encrypt credentials.
- Data: `opkeys.0002` adds audit records; retention is seven days via the optional local worker. The original notes record migration and configuration precautions.

## Implementation boundaries

1. Core persists hashed keys, validates operations and administrator status, revokes or expires sessions, and writes audit events.
2. BFF checks operation keys against Core without caching and forwards verified administrative context. JWT remains available where supported.
3. Frontend stores login session data, uses OpKey when enabled and authorized, and requests revocation on logout.
4. The retention worker purges audit events older than seven days. Expiration is audited on observation, and key-holder identity is inferred.

## Verification strategy

- Run focused Core and integration tests with isolated synthetic data.
- Run `npm run test:e2e:opkey` from the frontend against the local stack. It creates a synthetic administrator in the active local database, removes the account and sessions, and preserves audit events; it does not perform payments or product mutations.
- The owner accepted the remaining earlier Playwright coverage as unverified. Future execution must use an isolated environment; do not run tests that mutate catalog or record credentials against the active database.
- Record actual results and outstanding exceptions; no commit, deployment outside local, or change to JWT revocation is implied by this plan.

## Spec Kit selection

This feature is in the Core repository. To target it for the prerequisite script without changing the current branch or the orchestrator's feature selection, run from the Core repository:

```bash
SPECIFY_FEATURE=008-operations-keys-auth SPECIFY_FEATURE_DIRECTORY=specs/008-operations-keys-auth \
  .specify/scripts/bash/check-prerequisites.sh --json --require-tasks --include-tasks
```

The environment variable selects the feature for the script; it does not rename or create a Git branch.

## Constitution check

Core owns persistence, authorization, and audit; GraphQL and BFF contracts remain at their existing boundaries. Preserve credentials, transactional integrity, and observed verification evidence. No new runtime architecture is proposed by these retrospective artifacts.