# Project Agent Instructions

## Continuity is mandatory

Read HANDOFF.md's current position first, then the active plan and applicable project instructions. Keep continuity current without being asked.

Checkpoint after each coherent implementation milestone, and before ending a session, becoming blocked, or handing work off. Capture important decisions and failed approaches promptly so they are not lost. A milestone is meaningful progress, not every individual edit or command. Update related documents together:

1. **HANDOFF.md**: what changed, paths, actual verification commands/results, partial work, blockers, and concrete next actions. Preserve useful history and mark superseded information as historical.
2. **Active plan under `.opencode/plan/`**: update assigned story progress, decision reasons, next step, blockers, and quality status. Update the existing plan rather than creating a competing one.

A task is not finished while either document is stale. COMPLETE for an implementation story means awaiting independent quality review, not whole-project completion. Do not start unassigned stories or invent missing requirements.

AGENTS.md holds persistent project instructions, commands, and constraints, not per-story progress. Preserve unrelated instructions and change it only when those instructions actually change.

## Handoff structure

Keep HANDOFF.md concise:

- **Current position** — what is true now, active story/stage, and the active plan path.
- **Completed** — finished work and relevant paths; distinguish implemented from quality-approved.
- **In progress** — partial changes and pending child work that must not be duplicated.
- **Next steps** — ordered concrete actions or the prerequisite needed to continue.
- **Blockers / open decisions** — category, evidence, and missing input or access.
- **Verification** — exact commands and outcomes; distinguish runtime checks from inspection or assumptions.

## Plans and orchestrator state

Plans are Markdown under `.opencode/plan/`, with scope, architecture, acceptance criteria, dependency-ordered story checklists, next action, and decision log. Preserve useful history; do not append full transcripts or create pipeline/status databases.

The orchestrator owns the active plan's `## Orchestrator checkpoint`: approval evidence, selected agents, stage, dispatch/results, retry/replanning counts, findings, and next action. Other agents preserve those fields while updating their assigned story. Never reset counters, manufacture approval, or mark unrelated stories complete. Do not store secrets in continuity files.

## Visual evidence

Do not read image files directly, regardless of the selected model. For screenshots, images, diagrams, or rendered UI, call the read-only `vision` subagent with the local path(s), relevant context, and a focused question. Treat its report as an observation, not direct visual verification.

## Project-specific guidance

Add the established stack, setup/test commands, architecture references, and constraints here as they become known. Do not invent commands or copy unrelated project conventions.
