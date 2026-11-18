---
name: ad-mqtt-intake
description: "Investigate bugs or ideas and file actionable, duplicate-checked GitHub issues for ad-mqtt, with project-scoped Empirica evidence."
---

# ad-mqtt /intake

Investigate and file work for ad-mqtt. Read `docs/agent-workflow.md` and `.agent-workflow.json`, then bootstrap this checkout and then verify its Empirica identity. Accept `bug "title"`, `idea "title"`, or an ordinary request to document work.

Search issues in `kars85/ad-mqtt` first. A bug needs observed/expected behavior, reproducible evidence or an explicitly named missing reproduction, and a bounded acceptance test. An idea needs the requested outcome, evidence, constraints, alternatives that affect the decision, and explicit scope. Ask only for missing facts that change the work.

Create or update the controlling issue under the user's work-documentation authorization. Use a structured GitHub tool or a uniquely named temporary body file with `gh issue create --repo kars85/ad-mqtt --body-file PATH`; never interpolate prose into shell source. Include `<!-- intake-schema: v1 -->`, independently verifiable `- [ ] [P1] ...` items (adjust priority to evidence), dependencies, verification, and remaining uncertainty. Only apply labels that exist; labels are optional.

For multi-project work, file linked issues in the owning repositories. Epic parents coordinate children; child issues own executable tasks. Validate the returned URL/repository, record the durable finding with this project's Empirica UUID, and return the issue link. Empirica bookkeeping failure must not duplicate a successful GitHub write. Preserve event payload contracts with alarmdecoder and ser2sock. Prefer synthetic fixtures; connecting to or controlling alarm equipment needs explicit task scope.
