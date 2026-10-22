# BGG Shop Core Constitution

## Scope and precedence

This constitution governs implementation in `bgg-shop-core`. The root `.specify/memory/constitution.md` governs shared product and cross-service behavior; this file governs Core's local implementation and cannot redefine that behavior. Existing Core behavior remains the compatibility baseline. No constitution automatically authorizes a retroactive behavior or contract change.

## Principles

### 1. Own domain behavior in Core

Keep Core-owned domain rules and persistence responsibilities in this service, including the GraphQL operations for identity, catalog, reviews, cart, checkout, orders, and invoices. Do not shift domain ownership to the BFF or Frontend. Follow the applicable `specs/*/spec.md` service specification and its linked canonical root specification.

### 2. Treat GraphQL SDL as the contract

`specs/schema.graphql` is the Core GraphQL contract. An explicitly approved contract change must keep that schema, runtime behavior, affected service specifications, tests, and downstream BFF mapping traceable and compatible. Do not replace the GraphQL boundary with a local REST contract or treat the BFF OpenAPI file as Core's contract.

### 3. Preserve integrity and protect data

Validate inputs and enforce authorization at the Core boundary according to the documented behavior. Preserve data integrity for persisted domain state, including cart, order, payment, inventory, and invoice behavior. Keep credentials and sensitive data out of public errors and responses.

### 4. Verify Core changes

For each approved change, trace the requirement through the Core service specification, GraphQL contract, implementation, relevant tests under `tests/`, and documentation. Run the applicable checks and report only observed results; do not represent a task status as evidence that tests passed.
