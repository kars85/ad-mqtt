---
name: ad-mqtt-abc
description: "Plan, implement and validate GitHub issue work in ad-mqtt using its A/B/C handoff and Empirica project."
---

# ad-mqtt /abc

Execute the A/B/C workflow for ad-mqtt. Read `docs/agent-workflow.md` and `.agent-workflow.json` from the assigned checkout before acting. Run the workflow helper's bootstrap/verify/session commands. Use only `kars85/ad-mqtt` and its configured Empirica UUID.

Resolve the requested issue in that repository. If none is supplied, inspect ready open issues and select only work covered by the user's request. Work one independently verifiable checklist item through PM-HANDOFF, DEV-EXECUTION and PM-VALIDATION. Claude plans/reviews and Codex implements when both runtimes are available. Read local CLI help to invoke them; never silently substitute an unavailable runtime or reviewer.

Use the profile's actual checks plus affected-area guidance. For --loop, continue ready items in the selected issue; for --loopall, continue authorized ready issues. Stop when the selected scope is accepted, no authorized ready work remains, or a concrete external decision blocks progress. Report unresolved dependencies without marking them done.

Keep issue acceptance, live system changes and merge/release authorization distinct. Record the exact state reached and link the issue/PR. Preserve event payload contracts with alarmdecoder and ser2sock. Prefer synthetic fixtures; connecting to or controlling alarm equipment needs explicit task scope.
