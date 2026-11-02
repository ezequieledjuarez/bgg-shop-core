# Tasks: Customer Authentication (CU-C01)

**Input**: [spec.md](spec.md), [Core SDL](../schema.graphql), and [CU-C01 ODD record](../../../odd/tasks/cu-c01-layered-bff.md)
**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md)

**Status**: Closed and validated with exceptions on 2026-10-30, with owner approval. The live guest-cart merge remains unverified.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Parallelizable work on independent files.
- **[Story]**: The corresponding Core authentication outcome.
- All items below describe completed/as-built work; checked status does not imply a check was run.

## Phase 1: GraphQL Authentication Contract and Behavior

**Purpose**: Provide Core registration and credential login using Django identity.

- [x] T001 [US1] Define `RegisterInput`, `LoginInput`, `User`, token/error payloads, and `register`/`login` mutations in [specs/schema.graphql](../schema.graphql) and [saleor/graphql/api.py](../../saleor/graphql/api.py).
- [x] T002 [US1] Implement email normalization, duplicate-email error mapping, Django user creation/password hashing, generic invalid-credential handling, and login token issuance in [saleor/graphql/api.py](../../saleor/graphql/api.py).
- [x] T003 [US1] Keep password fields out of the public user type and exercise the authentication schema contract in [tests/test_graphql_auth.py](../../tests/test_graphql_auth.py).
- [x] T004 [US1] Record the Core-focused verification status: pending verification in the CU-C01 ODD record; the associated ODD record reports BFF checks only. Do not infer Core execution from non-ODD documentation.

## Dependencies & Execution Order

- T001 defines the contract consumed by T002; T003 checks the exposed schema shape.
- T004 records verification provenance and does not change the completed implementation status.

## Implementation Strategy

As-built Core scope only. The ODD record for CU-C01 is about BFF layering and does not report a historical Core test command, pass count, or Core commit; those historical items remain unverified. The later focused check below is separate evidence.

On 2026-10-30, `python -m pytest -q -p no:cacheprovider tests/test_graphql_auth.py tests/test_graphql_cart.py::test_login_merges_guest_cart_only_after_account_authentication --tb=short` passed 3 tests with one warning. The merge test uses mocks and does not verify live PostgreSQL behavior.
