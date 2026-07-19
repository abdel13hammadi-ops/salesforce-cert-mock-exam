# CertBound Project Simulation — Engineering Brief

**Adopted copy.** This is the corrected, adopted counterpart of `scenario_content/source/ENGINEERING_BRIEF.md` (which remains unchanged for provenance). This copy applies documentation corrections identified during content normalization (SIM-CONTENT-01); it does not change the intended gameplay design.

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

All simulation content lives in JSON files conforming to the adopted `simulation.schema.json` (see `scenario_content/schemas/1.0.0/simulation.schema.json`). **Content is fully separated from code.** The engine must be able to load and play ANY file matching that schema — do not hardcode anything about the BA-201 storyline (or any other scenario's storyline) into the engine itself. This is what lets CertBound reuse the same engine for other certifications (Administrator, Platform App Builder, Sales Cloud Consultant, Service Cloud Consultant, and beyond) later by just adding new JSON files.

A simulation is a **graph of scenes**, not a linear array, and the schema places no minimum or maximum on how many scenes a scenario version may contain — different scenarios, or later versions of the same scenario, may have entirely different scene counts. Each scene has:
- `narrative` — the story text
- `decision.options[]` — 2-4 choices, each with its own `nextScene` pointer

### Certification identity fields (corrected in this adopted copy)

Each scenario file declares two separate, non-interchangeable certification identity fields, not one:

- `certificationExamName` — local scenario content stores this value for deterministic lookup against the database's `certifications.exam_name` column (the same real, authoritative lookup/validation identity used elsewhere in CertBound — see `utils/certification_context.py` and `workers/certification_registry.py`'s `*_EXAM_NAME` constants). Named for the exam-name-shaped lookup string it actually holds, not a short code — the registry's own separate, display-only certification-code column must not be confused with this field.
- `examCode` — stores the external, candidate-facing exam identifier (e.g. `"BA-201"`), used only for display and content-authoring cross-reference, never for internal lookups.

Future database ingestion resolves `certificationExamName` to the authoritative `certifications.id` foreign key at publish/import time. Persisted scenario and attempt records (once persistence exists) must use that resolved database ID going forward, rather than permanently re-resolving the display-text `certificationExamName` value on every read — the display text is a stable, human-readable lookup key at content-authoring time, not a substitute for a real foreign key at runtime.

Multiple options can point to the **same** `nextScene` (the story reconverges — this is the normal case for most scenes, since most wrong answers don't need a whole separate detour, just a worse `feedback` and worse `stateChanges`). A few key scenes route wrong answers to a genuinely different **detour scene** (`isDetour: true`) that shows a consequence narrative before rejoining the main spine. In the BA-201 sample this is limited to 4 places — don't expect every wrong answer to branch the story; most just affect state and feedback text while continuing the same path. Note that a detour scene does not automatically add an extra scene to a candidate's path length — whether it does depends on the specific transition graph around it and must be verified by reachability/path-length analysis, not assumed from the `isDetour` flag alone.

## 3. State model

`initialState` in the JSON defines starting values, and the adopted schema now additionally requires a top-level `stateVariables` array declaring, for every state variable used anywhere in the scenario, its `key` and its clamp `minimum`/`maximum` (either bound may be omitted if the variable is unbounded in that direction). The engine should:

- Initialize a mutable state object from `initialState` when a candidate starts a simulation.
- On each option selection, apply that option's `stateChanges` deltas.
- **Clamp every state variable to its declared `stateVariables` bounds after every single update, not just at the end.** This is load-bearing, not cosmetic, and the engine must derive clamp bounds generically from `stateVariables` — it must never hardcode a variable name such as `projectHealth` anywhere in engine code. In the adopted BA-201 content, `stateVariables` declares: `projectHealth` (minimum 0, maximum 100), `stakeholderTrust` (minimum 0, maximum 100), and `scheduleRisk` (minimum 0, no maximum — a floor only, to avoid a negative-risk display, which would read oddly in the UI).
- **Important test-design note (corrected in this adopted copy):** BA-201's own "100%-correct" and canonical "0%-correct" playthroughs (see Section 10) do **not**, in this specific content, ever produce a different result under per-step clamping versus clamping only once at the very end — neither path's running total ever actually crosses a bound mid-sequence for THIS content. Do not rely on those two content-level regression tests alone to prove the engine clamps per-update rather than only at the end; a separate, content-independent synthetic unit test (feeding a delta sequence that deliberately crosses a bound mid-sequence) is required to actually distinguish and prove correct per-update clamping behavior.
- Append any `setFlags` strings to a `flags: string[]` array in state (flags are for future conditional narrative — the engine doesn't need to interpret them now beyond storing them).
- Persist this state across the whole playthrough, with backend resume support recommended given realistic completion times of 45-75 minutes.

## 4. Navigation logic

- Start at `startScene`.
- Render that scene's `narrative` + `decision.prompt` + `decision.options`.
- On selection:
  1. Show that option's `feedback` immediately (short, in-the-moment reaction).
  2. Apply `stateChanges` / `setFlags`.
  3. Show the scene-level `explanation` (the deeper "why," same depth as a normal MCQ explanation) — this can be combined into one feedback panel with the option feedback, doesn't need a separate click.
  4. On "Continue," navigate to that option's `nextScene`.
- If `nextScene === "EVALUATE_ENDING"`, skip directly to ending evaluation. **This is now the ONLY accepted terminal sentinel value in the adopted contract.** The originally-imported BA-201 content actually used the undocumented literal `"ENDING"` for its two terminal transitions (in `s24_golive_readiness`); this has been corrected to `"EVALUATE_ENDING"` in the adopted scenario content (`scenario_content/business_analyst/ba201-sim-meridian-health-01/1.0.0/scenario.json`). The engine's content validator must reject any `nextScene` value that is neither an existing scene id nor exactly `"EVALUATE_ENDING"`, rather than silently tolerating alternate spellings.
- When the candidate reaches the terminal sentinel, evaluate `endings[]` **strictly in array order** against final state — first matching condition wins. Render that ending's `narrative` + `scoreBand`. This ordering is correctness-critical (the BA-201 endings array is ordered from strictest condition to a near-universal catch-all fail condition) and must never be reordered, sorted, or evaluated as a "best match."

## 5. Domain progress rail (UI)

The top-level `domains[]` array is ordered to match the certification's official domain weighting and the order the simulation walks through them. Each scene has a `domainId`. Render a horizontal progress rail (similar to a multi-step checkout flow) showing which domain the candidate is currently in, with completed domains marked done. This reinforces that the simulation maps 1:1 onto the real exam's domain structure — good for trust/credibility with the candidate.

**Progress-percentage caution:** because branching means authored scene count, reachable scene count, and any single attempt's actual path length can all legitimately differ, the UI must not compute progress as "scenes visited ÷ total authored scenes." For BA-201 specifically, even a perfect playthrough visits only 24 of the 28 authored scenes — never all 28 — so a naive authored-count-based percentage would never reach 100% even on a flawless run. Use the domain rail, or a per-version precomputed typical-path-length range, instead.

## 6. Scoring & review recommendations

At the end, in addition to the ending's fixed `narrative`/`scoreBand`, compute and show:
- A simple **per-domain accuracy breakdown** (e.g., "Customer Discovery: 3/4 correct decisions") by tracking which `domainId` each chosen option's scene belonged to, and whether `isCorrect` was true.
- A **"Review recommended"** list of domains where accuracy was below some threshold (e.g., <70%), so the candidate has an actionable next step — direct them back to the relevant MCQ volume(s) on the site for that domain.

This is the main place where the simulation should integrate with the rest of CertBound — it's the bridge that sends a candidate from "I struggled with Stakeholder Collaboration in the simulation" back to your existing Volume 1-4 MCQ content for that domain.

## 7. What NOT to build in v1 (scope guardrails)

- No true multi-path "graph with many cycles" — keep it a forward-moving spine with short detours that reconverge, exactly as authored. Don't generalize the engine to support loops/backtracking; the content doesn't need it, and cycles must be rejected at content-validation time in V1.
- No server-side grading/anti-cheat needed for v1 — this is a self-study tool, not a proctored exam.
- No need to support skipping ahead or jumping to arbitrary scenes — it's meant to be played start to finish in order, like the real project lifecycle it simulates.
- Don't try to make `multi_select` decision type fully generic on day one if it adds significant complexity — the BA-201 sample is `single_select` throughout. The schema supports `multi_select` for future content, but you can stub/defer that decision-type's UI until a simulation actually uses it.

## 8. File manifest accompanying this brief

| File | Purpose |
|---|---|
| `simulation.schema.json` (adopted: `scenario_content/schemas/1.0.0/simulation.schema.json`) | The JSON Schema contract — validate all simulation content files against this. |
| `ba201-sim-meridian-health-01.json` (adopted: `scenario_content/business_analyst/ba201-sim-meridian-health-01/1.0.0/scenario.json`) | One fully-written, ready-to-ship BA-201 simulation: **28 scenes** (corrected — the originally-imported brief incorrectly stated 26 here while its own Section 10 correctly referred to "this 28-scene simulation"; the actual content has always contained 28), 4 short detours, all 6 domains, 4 possible endings. |
| This file (`ENGINEERING_BRIEF.md`) | This document. |

## 9. Suggested first implementation pass for Cursor

1. Validate `ba201-sim-meridian-health-01.json` (adopted copy) against the adopted `simulation.schema.json` at build time or in a test, so any future content files fail fast if malformed.
2. Build a scenario player component that:
   - Loads the adopted scenario content for a given `(certificationExamName, simulationId, version)`.
   - Holds `currentSceneId` and `state` (derived from `initialState` + `stateVariables` clamp bounds).
   - Renders the current scene, options, feedback, explanation, "Continue" button.
   - On finish, evaluates endings and renders the results screen with per-domain breakdown.
3. Style pass: progress rail, state-variable mini-dashboard (e.g., a small "Project Health: 78" meter visible throughout, since watching it move is a big part of what makes this feel different from a normal quiz).
4. Ship the BA-201 simulation behind a feature-flagged route, linked from the existing BA-201 volume pages ("Ready to test your judgment on a full project? Try the simulation →").

## 10. Content QA — verified scoring calibration (write a regression test against this)

The `ba201-sim-meridian-health-01.json` content was numerically simulated (in Python, outside the actual app) across the full skill spectrum to confirm the score bands behave sensibly, **assuming correct per-step clamping as described in Section 3**. These are the verified reference numbers — if an implementation produces meaningfully different results for the same input choices, the clamping logic is probably wrong somewhere, not the content:

| Candidate behavior | Expected final `projectHealth` | Expected ending |
|---|---|---|
| Picks the correct option in every scene | Exactly 100 | `ending_distinction` |
| Picks the **canonical** incorrect option in every scene | 19 | `ending_fail` |
| Picks correctly ~70% of the time (realistic strong candidate) | Typically 70-100 | `ending_distinction` or `ending_pass` |
| Picks correctly ~50% of the time (realistic average candidate) | Typically 58-98, most commonly high-70s/low-80s | Usually `ending_pass`, sometimes `ending_distinction` or `ending_marginal` |
| Picks correctly ~15-30% of the time (struggling candidate) | Typically 19-62 | Usually `ending_marginal` or `ending_fail` |

**Canonical "all incorrect" selection rule (clarified in this adopted copy):** exactly one scene in this content (`s01_kickoff`) offers more than one `isCorrect: false` option. The "picks the incorrect option in every scene" regression path is only well-defined, and only reproduces the `19` figure above, when the candidate is defined as choosing the **first authored incorrect option, in options-array order**, at any scene offering more than one wrong choice. Choosing a different incorrect option at such a scene will still reach `ending_fail`, but will not necessarily reproduce the exact `19` value — for example, choosing the second incorrect option at `s01_kickoff` in this content yields `18`, not `19`. Any regression test asserting the exact value `19` must pin this selection rule explicitly, not just "always pick an incorrect option."

**Per-update clamping test-design note (added in this adopted copy):** the two deterministic paths in this table (all-correct, canonical-all-incorrect) do not, for this specific content, ever produce a different final result under per-step clamping versus end-of-run-only clamping — their running totals never actually cross a declared bound mid-sequence. These two tests remain valuable as content-level regressions, but a **separate, content-independent synthetic unit test** (applying a delta sequence engineered to cross a bound mid-sequence) is required to actually prove the engine clamps after every update rather than only once at the end.

If you add a new simulation later (different cert, different scene count), it will need its own calibration pass — don't assume the same delta magnitudes will produce the same distribution for a simulation with a different number of scenes. As a rule of thumb that held for this 28-scene simulation: keep the total possible "all correct" gain to roughly 50-60% of the 0-100 range, and make the total possible "all incorrect" loss noticeably larger in magnitude than the gain (mistakes should cost more than correct answers earn) — that asymmetry is what produces a believable, discriminating spread across skill levels rather than everyone clustering at the top.
