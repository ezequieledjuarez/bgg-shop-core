# Implementation Plan: Customer Authentication (CU-C01)

**Branch**: `CU-06-wishlist` | **Date**: 2026-10-21 | **Spec**: [spec.md](spec.md)
**Input**: Core authentication specification in [spec.md](spec.md)

## Summary

Retrospective plan for the Core portion of CU-C01. The Core exposes GraphQL registration and login using Django's built-in user model, normalizes email for identity lookup, hashes passwords through Django user creation, and issues signed access/refresh tokens on successful login. The reference contract is [specs/schema.graphql](../schema.graphql); browser session handling and logout presentation are outside this service.

## Technical Context

**Language/Version**: Python (exact runtime version not reported in the Core CU-C01 ODD record)  
**Primary Dependencies**: Django, Graphene, JWT support (the exact JWT package/version is not reported in the Core CU-C01 ODD record)  
**Storage**: Django authentication user model backed by PostgreSQL  
**Testing**: pytest with Django setup; `tests/test_graphql_auth.py`  
**Target Platform**: Linux server  
**Project Type**: Django/Graphene web service  
**Performance Goals**: Not specified in the retrospective source; pending verification.  
**Constraints**: Do not expose passwords or password hashes; registration does not create a session; invalid credentials use a generic public error; no server-side logout or refresh-token revocation is evidenced.  
**Scale/Scope**: GraphQL register/login operations and their public schema.

## Constitution Check

Retrospective fit: the feature uses the existing Django identity model and Graphene API rather than introducing a parallel user store. Core owns identity validation and token issuance; BFF and browser session responsibilities remain in their existing service boundaries. No extra repository, migration framework, or dependency-injection layer is represented in the Core feature scope.

## Project Structure

### Documentation (this feature)

```text
specs/001-authentication/
├── spec.md
├── plan.md
└── tasks.md
```

### Source Code (Core repository)

```text
specs/schema.graphql
saleor/graphql/api.py
tests/test_graphql_auth.py
```

**Structure Decision**: The Core's Graphene schema and resolvers are in `saleor/graphql/api.py`; `specs/schema.graphql` is the reference SDL and the focused Core schema tests are in `tests/test_graphql_auth.py`.

## Complexity Tracking

No additional Core abstraction or architecture violation is reported for this feature. Runtime/database claims not present in the ODD evidence are left pending rather than inferred.
