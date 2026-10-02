---
name: api-implementation-guard
description: Implement or modify AX-WMS Spring Boot backend features while identifying concrete risks, resolving them within scope, and reporting remaining concerns. Use for API domain, service, repository, controller, security, and database changes; not for web UI-only work.
---

# API Implementation Guard

Implement the requested backend change while acting as a practical risk reviewer. Raise only concerns supported by the request or codebase, address them when they are in scope, and make any remaining trade-off visible.

## Before editing

- Read applicable `AGENTS.md`, `docs/api/adr.yaml`, and `docs/api/code-convention.yaml` before modifying `api/` code.
- Trace the affected request flow enough to identify callers, authorization boundaries, persisted data, and externally visible API contracts.
- State concise implementation assumptions when requirements are ambiguous. If an assumption materially changes business behavior, stop and ask the user rather than choosing it silently.
- Identify only concrete risks, labeling them `차단` when implementation must not proceed safely and `주의` when a scoped mitigation is available.

## While implementing

- Keep changes within the requested backend scope. Do not add UI work or unrelated refactors.
- Consider the relevant subset of: authentication/authorization, input validation, domain-state validity, transaction boundaries, error-response consistency, concurrency or duplicate-write behavior, query volume and pagination, and backward compatibility.
- Prefer existing project patterns and ADR decisions. New or changed helper, utility, factory, normalization, validation, and `*OrThrow` methods must follow the API Javadoc rules.
- Do not insert reference data through Flyway migrations. Keep test-only or explicit development data separate from schema migration.
- If a risk cannot be fixed without expanding the request, leave the implementation scoped and report the exact decision or authority needed.

## Verification and handoff

- Run the narrowest relevant tests or checks available. Do not claim checks that were not run.
- Report, in Korean: implemented behavior; concrete concerns found and how each was handled; unresolved risks or decisions; and verification performed with remaining test scenarios.

Avoid speculative problem lists, vague warnings, and rewriting code solely to eliminate stylistic concerns.
