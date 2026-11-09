# Tasks: Admin Navigation and Discoverability — Core

**Master backlog**: [orchestrator tasks](../../../specs/014-admin-navigation/tasks.md)<br>
**Status**: Investigation complete; no Core contract or runtime implementation is planned.<br>
**Scope**: Local environments only.

This service document contains only Core-owned tasks. Task IDs, statuses, and dependencies match the master backlog. There is deliberately no proposed Core SDL change: the existing `Query.me` returns `User` with `isActive`, `isStaff`, and `isSuperuser`.

## Core-owned tasks

- [x] **T001 [Core] [DONE — investigation only]** Confirm whether Core already exposes current identity and role data needed by the navigation capability response. **Evidence**: `specs/schema.graphql:427-429, 554-561` defines `Query.me` and `User.isActive/isStaff/isSuperuser`; `saleor/graphql/api.py:1460-1464` resolves the current authenticated user. No Core source, SDL, migration, or test changed. **Trace**: FR-002, FR-011; US4.

## Core execution boundary

- No Core behavior task is planned. BFF should reuse existing `Query.me`; Frontend must not call Core directly.
- If T005/T006 validation shows that existing Core identity behavior cannot supply current role flags, stop and propose a separate explicit Core contract/task before changing SDL or runtime behavior.
- No migration or database changes are in scope.
- Core verification of existing identity behavior may use the local pytest suite; no Core test was run as part of this planning-only delivery.

## Key Learnings

1. Core already exposes current active, staff, and Superadmin flags through its authenticated `Query.me` result.
2. A BFF-only projection avoids duplicating the current-user contract in the Core GraphQL schema.
