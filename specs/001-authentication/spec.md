# Core Functional Specification: Customer Authentication (As-Built)

**Status**: As-built, retrospective
**Canonical use case**: [CU-C01 — Customer Authentication](../../../specs/001-authentication-as-built/spec.md)

## Problem and objective

The Core layer provides GraphQL account registration and credential authentication for the end-to-end CU-C01 flow. Its boundary is identity validation and authentication data; browser session presentation and local logout belong to other layers.

## Core requirements and acceptance criteria

- **CORE-AUTH-001 — Register**: `register` accepts email, password, first name, and last name. Core normalizes the email for account lookup/storage and reports duplicate email through the payload's user errors. A successful registration returns the public user fields and no authentication tokens.
- **CORE-AUTH-002 — Authenticate**: `login` accepts email and password. Invalid credentials and inactive accounts receive the generic `INVALID_CREDENTIALS` response; a successful login returns the user and signed access/refresh tokens.
- **CORE-AUTH-003 — Keep credentials private**: The GraphQL `User` type does not expose a password.
- **CORE-AUTH-004 — Optional guest-cart handoff**: `login` can accept `guestCartId`; after authentication Core attempts to merge that guest cart into the account cart. The cart merge rules are specified in [Core CU-C04](../004-shopping-cart/spec.md).

## GraphQL contract

- `Mutation.register(input: RegisterInput!): RegisterPayload!`
- `Mutation.login(input: LoginInput!): LoginPayload!`

The `User`, `AuthToken`, and `UserError` payload types define returned identity, token, and validation data in the [Core SDL](../schema.graphql).

## Limitations and exclusions

- Core exposes no logout or token-revocation operation. CU-C01 local browser logout is outside this layer.
- Registration does not automatically issue tokens. Password recovery, email confirmation, social login, refresh-token rotation/revocation, and server-side logout are not evidenced as implemented here.
- Core tests verify schema fields and absence of a password field; the existing authentication tests do not exercise registration or credential authentication against a database.

## Evidence

- Contract and implementation: [GraphQL SDL](../schema.graphql), [Core GraphQL implementation](../../saleor/graphql/api.py).
- Tests: [Core authentication schema tests](../../tests/test_graphql_auth.py); login's guest-cart handoff is covered in [Core cart tests](../../tests/test_graphql_cart.py).
