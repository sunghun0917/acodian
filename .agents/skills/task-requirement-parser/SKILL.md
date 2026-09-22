---
name: task-requirement-parser
description: Turn a numbered product requirement or specification into an implementation-ready AX-WMS backend brief. Use before implementing an API task; do not use for UI-only work or after the scope is already fully specified.
---

# Task Requirement Parser

Convert the selected requirement into a small, reviewable backend brief. Preserve the user's intended scope; do not invent product features to fill a gap.

## Read and classify

- Read the supplied requirement source and identify the exact numbered item being implemented.
- Inspect relevant existing APIs, domain models, and `api/` conventions only as far as needed to distinguish a new capability from an extension of existing behavior.
- Separate stated facts from assumptions. An omitted behavior is not automatically a requirement.

## Produce the implementation brief

Before changing code, summarize in Korean:

1. **목표와 범위** — the user-facing outcome and explicitly excluded work;
2. **API 계약** — endpoint, method, actor, request fields, response shape, and stable error cases known from the requirement or existing conventions;
3. **도메인·데이터** — entities, state transitions, ownership, storage impact, and integration points;
4. **완료 조건** — observable success and failure cases;
5. **확인 필요 사항** — only decisions that materially change behavior, such as permission rules, lifecycle ownership, retention, or incompatible API behavior.

Use `차단` only when a missing decision makes a safe implementation impossible. For routine details that existing project patterns resolve, state the chosen pattern and proceed.

## Handoff

- Recommend the smallest backend implementation slice that delivers the selected item.
- When implementation is requested, hand the brief to the normal API implementation workflow. Do not create UI requirements, schema changes, or integrations that the source does not support.
- Keep the brief in the response unless the user explicitly asks to save it as a project document.
