# CertBound Project Simulation — Engineering Brief

**Audience:** ChatGPT (planning/codegen) and Cursor (implementation), building this into certbound.com.
**Goal:** A reusable simulation engine that can run ANY certification's branching case-study content, starting with the BA-201 simulation included alongside this brief (`ba201-sim-meridian-health-01.json`).

---

## 1. What this feature is

Today, certbound.com (presumably) offers standard multiple-choice question banks. This is a **different exam format**: one continuous fictional project that a candidate walks through scene by scene, making a decision at each step. Decisions:

- Show immediate feedback (right/wrong + short reason)
- Mutate a small set of running **state variables** (e.g., `projectHealth`, `stakeholderTrust`, `scheduleRisk`)
- Route the candidate to a specific **next scene** — sometimes the same next scene regardless of which option was picked (the story continues either way, just colored differently), sometimes genuinely different scenes (a "detour" caused by a bad decision, which always eventually reconverges with the main spine)
- At the end, final state values are evaluated against a list of **endings**, and the candidate gets a narrative wrap-up + a pass/fail-style score band, similar in spirit to "Pass with distinction / Pass / Marginal / Fail."

Think of it as a lightweight, text-based "flight simulator" for a certification's day-to-day judgment calls, rather than a quiz.

## 2. Content format (read this first)

All simulation content lives in JSON files conforming to `simulation.schema.json` (included). **Content is fully separated from code.** Cursor should build a generic engine that can load and play ANY file matching that schema — do not hardcode anything about the BA-201 storyline into the engine itself. This is what lets CertBound reuse the same engine for PMP, AWS, CompTIA, etc. later by just adding new JSON files.

A simulation is a **graph of scenes**, not a linear array. Each scene has:
- `narrative` — the story text
- `decision.options[]` — 2-4 choices, each with its own `nextScene` pointer

Multiple options can point to the **same** `nextScene` (the story reconverges — this is the normal case for most scenes, since most wrong answers don't need a whole separate detour, just a worse `feedback` and worse `stateChanges`). A few key scenes route wrong answers to a genuinely different **detour scene** (`isDetour: true`) that shows a consequence narrative before rejoining the main spine a scene or two later. This is intentional and limited to ~4 places in the BA-201 sample — don't expect every wrong answer to branch the story; most just affect state and feedback text while continuing the same path.

## 3. State model

`initialState` in the JSON defines which numeric variables exist and their starting values (typically 0-100 scales, like `projectHealth: 100`). The engine should:

- Initialize a mutable state object from `initialState` when a candidate starts a simulation
- On each option selection, apply that option's `stateChanges` deltas
- **Clamp `projectHealth` and `stakeholderTrust` to the range [0, 100] after every single update, not just at the end.** This is load-bearing, not cosmetic: the content in `ba201-sim-meridian-health-01.json` was numerically balanced assuming per-step clamping to 0-100. If the engine only clamps once at the very end (or doesn't clamp at all), the score will not produce the intended distribution across candidate skill levels — verified during content QA that a 100%-correct playthrough should land at exactly 100 (Distinction), a 0%-correct playthrough should land at 19 (Fail, due to one early positive-default scene), and a realistic ~70%-correct candidate should land in the 70-100 range (Distinction/Pass boundary). Other state variables (like `scheduleRisk`) are not used in any ending condition in the BA-201 sample and don't need a ceiling — only a floor at 0 to avoid a negative-risk display, which would read oddly in the UI.
- Append any `setFlags` strings to a `flags: string[]` array in state (flags are for future conditional narrative — the engine doesn't need to interpret them now beyond storing them, though some scene narratives may reference "if you did X earlier" purely in their authored text rather than true conditional rendering; that's fine for v1)
- Persist this state across the whole playthrough (in React state / context is fine for v1; persist to a backend/localStorage if you want resume support — recommended given completion time is 45-75 minutes)

## 4. Navigation logic

- Start at `startScene`
- Render that scene's `narrative` + `decision.prompt` + `decision.options`
- On selection:
  1. Show that option's `feedback` immediately (short, in-the-moment reaction)
  2. Apply `stateChanges` / `setFlags`
  3. Show the scene-level `explanation` (the deeper "why," same depth as a normal MCQ explanation) — this can be combined into one feedback panel with the option feedback, doesn't need a separate click
  4. On "Continue," navigate to that option's `nextScene`
- If `nextScene === "EVALUATE_ENDING"`, skip directly to ending evaluation (used for rare early-exit scenarios if you add any later; the BA-201 sample doesn't use this — it always reaches the final scene naturally)
- When the candidate finishes the last scene in the spine, evaluate `endings[]` **in array order** against final state — first matching condition wins. Render that ending's `narrative` + `scoreBand`.

## 5. Domain progress rail (UI)

The top-level `domains[]` array is ordered to match the certification's official domain weighting and the order the simulation walks through them. Each scene has a `domainId`. Render a horizontal progress rail (similar to a multi-step checkout flow) showing which domain the candidate is currently in, with completed domains marked done. This reinforces that the simulation maps 1:1 onto the real exam's domain structure — good for trust/credibility with the candidate.

## 6. Scoring & review recommendations

At the end, in addition to the ending's fixed `narrative`/`scoreBand`, compute and show:
- A simple **per-domain accuracy breakdown** (e.g., "Customer Discovery: 3/4 correct decisions") by tracking which `domainId` each chosen option's scene belonged to, and whether `isCorrect` was true
- A **"Review recommended"** list of domains where accuracy was below some threshold (e.g., <70%), so the candidate has an actionable next step — direct them back to the relevant MCQ volume(s) on the site for that domain

This is the main place where the simulation should integrate with the rest of CertBound — it's the bridge that sends a candidate from "I struggled with Stakeholder Collaboration in the simulation" back to your existing Volume 1-4 MCQ content for that domain.

## 7. What NOT to build in v1 (scope guardrails)

- No true multi-path "graph with many cycles" — keep it a forward-moving spine with short detours that reconverge, exactly as authored. Don't generalize the engine to support loops/backtracking; the content doesn't need it.
- No server-side grading/anti-cheat needed for v1 — this is a self-study tool, not a proctored exam.
- No need to support skipping ahead or jumping to arbitrary scenes — it's meant to be played start to finish in order, like the real project lifecycle it simulates.
- Don't try to make `multi_select` decision type fully generic on day one if it adds significant complexity — the BA-201 sample is `single_select` throughout. The schema supports `multi_select` for future content, but you can stub/defer that decision-type's UI until a simulation actually uses it.

## 8. File manifest accompanying this brief

| File | Purpose |
|---|---|
| `simulation.schema.json` | The JSON Schema contract — validate all simulation content files against this. |
| `ba201-sim-meridian-health-01.json` | One fully-written, ready-to-ship BA-201 simulation: 26 scenes, 4 short detours, all 6 domains, 4 possible endings. |
| This file (`ENGINEERING_BRIEF.md`) | This document. |

## 9. Suggested first implementation pass for Cursor

1. Write a Zod (or equivalent) schema in TypeScript mirroring `simulation.schema.json`, and validate `ba201-sim-meridian-health-01.json` against it at build time or in a test, so any future content files fail fast if malformed.
2. Build a `<SimulationPlayer simulationId="ba201-sim-meridian-health-01" />` component that:
   - Fetches/imports the JSON
   - Holds `currentSceneId` and `state` in React state (or a small reducer — a reducer is probably cleanest given the state-mutation pattern)
   - Renders the current scene, options, feedback, explanation, "Continue" button
   - On finish, evaluates endings and renders the results screen with per-domain breakdown
3. Style pass: progress rail, state-variable mini-dashboard (e.g., a small "Project Health: 78" meter visible throughout, since watching it move is a big part of what makes this feel different from a normal quiz)
4. Ship BA-201 simulation behind a new route, e.g. `/simulations/ba201/meridian-health`, linked from the existing BA-201 volume pages ("Ready to test your judgment on a full project? Try the simulation →")

## 10. Content QA — verified scoring calibration (write a regression test against this)

The `ba201-sim-meridian-health-01.json` content was numerically simulated (in Python, outside the actual app) across the full skill spectrum to confirm the score bands behave sensibly, **assuming correct per-step clamping as described in section 3**. These are the verified reference numbers — if Cursor's implementation produces meaningfully different results for the same input choices, the clamping logic is probably wrong somewhere, not the content:

| Candidate behavior | Expected final `projectHealth` | Expected ending |
|---|---|---|
| Picks the correct option in every scene | Exactly 100 | `ending_distinction` |
| Picks the incorrect option in every scene | 19 | `ending_fail` |
| Picks correctly ~70% of the time (realistic strong candidate) | Typically 70-100 | `ending_distinction` or `ending_pass` |
| Picks correctly ~50% of the time (realistic average candidate) | Typically 58-98, most commonly high-70s/low-80s | Usually `ending_pass`, sometimes `ending_distinction` or `ending_marginal` |
| Picks correctly ~15-30% of the time (struggling candidate) | Typically 19-62 | Usually `ending_marginal` or `ending_fail` |

A simple regression test: hardcode the "all correct" path (always pick the option where `isCorrect: true`) and assert final `projectHealth === 100` and final ending `id === "ending_distinction"`. Do the same for "all incorrect" and assert `projectHealth === 19` and `ending_fail`. These two deterministic paths are cheap to test and will catch most clamping/calculation regressions immediately.

If you add a new simulation later (different cert, different scene count), it will need its own calibration pass — don't assume the same delta magnitudes will produce the same distribution for a simulation with a different number of scenes. As a rule of thumb that held for this 28-scene simulation: keep the total possible "all correct" gain to roughly 50-60% of the 0-100 range, and make the total possible "all incorrect" loss noticeably larger in magnitude than the gain (mistakes should cost more than correct answers earn) — that asymmetry is what produces a believable, discriminating spread across skill levels rather than everyone clustering at the top.
