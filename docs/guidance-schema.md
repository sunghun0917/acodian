# Guidance Schema Contract

This document defines the canonical guidance schema and marker contracts for oh-my-codex (OMX) integration within the AX-WMS repository.

## Runtime Marker Contracts

When overlays, updates, or templates are applied to `AGENTS.md` or other system prompts, the following runtime markers must remain stable and non-destructive:

- `<!-- OMX:RUNTIME:START --> ... <!-- OMX:RUNTIME:END -->`
  - Defines the dynamic runtime instructions injected by the active OMX orchestrator.
- `<!-- OMX:TEAM:WORKER:START --> ... <!-- OMX:TEAM:WORKER:END -->`
  - Defines worker-specific lane instructions when operating in team/swarm mode.
- `<!-- OMX:AGENTS:START --> ... <!-- OMX:AGENTS:END -->`
  - Encapsulates generated agent guidance and autonomy directives.

## Guidance Sections

- `<operating_principles>`: Base operational posture and execution philosophy.
- `<delegation_rules>`: Authority bounds, delegation criteria, and lane choices.
- `<specialist_routing>`: Criteria for routing between explore, researcher, dependency-expert, and execution roles.
- `<verification>`: Verification requirements prior to completion claims.
