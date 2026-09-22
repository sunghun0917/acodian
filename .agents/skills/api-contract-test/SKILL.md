---
name: api-contract-test
description: Define and verify API contract tests for AX-WMS Spring Boot endpoint changes, including meaningful failure paths and authorization boundaries. Use for backend API additions or modifications; not for UI-only work or generic code review.
---

# API Contract Test

Protect the externally observable API contract of the requested backend change. Focus on behavior a client can rely on, rather than internal implementation details.

## Establish the contract

- Read applicable `AGENTS.md`, `docs/api/adr.yaml`, and `docs/api/code-convention.yaml` before modifying API tests or production code.
- Derive the expected endpoint behavior from the request, existing API patterns, and established error conventions.
- Identify the smallest relevant set of scenarios: success, validation failure, authentication or authorization failure, missing resource, invalid state transition, and duplicate or replayed write when applicable.
- Include pagination, ordering, or concurrent behavior only when the endpoint or domain behavior exposes it.

## Implement and verify

- Prefer the repository's existing test style and fixtures. Test at the narrowest layer that proves the client-facing behavior.
- Assert status, response contract, and externally meaningful state changes. Avoid tests coupled to private helper calls or incidental query order.
- Do not add test data through Flyway migrations; use fixtures or explicit test setup.
- Run the narrowest relevant checks available. If they cannot be run, state why and list the unverified scenarios without presenting them as passed.

## Report

Report in Korean: covered contract scenarios, tests or checks actually run, and any remaining contract decision that needs user input. Do not treat a missing product-policy decision as a test failure; surface it as a decision.
