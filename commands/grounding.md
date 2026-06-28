---
description: Toggle grounding attribution (citation policy + verifier) on or off
argument-hint: [on/off]
disable-model-invocation: true
---

!`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/grounding_spec.py --set "$ARGUMENTS"`

The current grounding-attribution state is shown above.

- `/grounding on` — enable the citation policy and the verifier
- `/grounding off` — disable both (the policy stops being injected and the verifier no-ops)
- `/grounding toggle` — flip the current state
- `/grounding` — show the current state without changing it

The setting is global and persists across sessions until you change it.
