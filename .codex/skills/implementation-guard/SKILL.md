---
name: implementation-guard
description: Implement or fix code by tracing callers, checking boundary contracts, and adding a focused regression check. Use for feature work, bug fixes, refactors, and external integrations; not for prose-only requests.
---

# Implementation Guard

Before editing, understand the complete execution path: the requested entry point, its callers, and the data or control-flow boundary it crosses. Search the repository for existing patterns and reuse the smallest one that fits.

## Contract checks

- List the observable inputs, outputs, failures, and side effects that the change must preserve.
- For an external API, SDK, database, queue, file format, or framework boundary, confirm the current contract from primary documentation or the installed source before coding. Do not infer a raw HTTP payload from an SDK convenience property.
- Treat untrusted or optional fields as absent, malformed, or reordered unless the contract guarantees otherwise.
- Preserve secrets: never include credentials, tokens, or full sensitive payloads in code, tests, or logs.

## Implementation

- Fix the shared root cause, not only the reported caller.
- Keep the change minimal. Do not add a dependency, abstraction, or configuration unless the current requirement needs it.
- Keep error messages actionable, but do not expose secrets or user data.

## Verification

- Add or update one focused test for each non-trivial changed behavior. Reproduce the boundary shape that caused the bug, including valid alternative ordering or optional fields when relevant.
- Run the narrowest relevant test, formatter, type check, or build available in the repository.
- If verification cannot run, report the exact missing prerequisite. Do not describe it as passing.
- Before finishing, compare the implementation and tests with the original requirement and state any remaining assumption or unverified external behavior.
