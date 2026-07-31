# SCENARIO_ENGINE_V2 — §17 Specification Alignment Report

**Task ID:** SIM-ENGINE-V2-SPEC-17-ALIGN-01  
**Date:** 2026-07-31  
**Model:** Composer 2.5 Fast  
**Scope:** Documentation and contract alignment only — no runtime changes.

---

## 1. Task status

**COMPLETE.** §17 ambiguity removed; both supported `optionDisplayPolicy` values are normatively defined; golden vector documented; targeted tests pass; protected paths untouched.

---

## 2. Files modified

| File | Change |
|---|---|
| `docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_SPEC.md` | Revision 6 — full §17 rewrite (§17.1–§17.9) |
| `docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_CUSTOM_VALIDATION.md` | Section G aligned to §17 revision 6 (counter encoding, both policies, golden vector reference) |

---

## 3. Files created

| File |
|---|
| `docs/scenario_simulator/SCENARIO_ENGINE_V2_SPEC_17_ALIGNMENT_REPORT.md` |

---

## 4. Specification revision

**Revision 6** of `SCENARIO_SCHEMA_1_1_0_SPEC.md` (supersedes revision 5).

Correction notice states this change **clarifies and freezes** already implemented SCENARIO_ENGINE_V2 behavior; it does **not** change runtime semantics.

---

## 5. Original ambiguity

Prior §17 stated:

```
stream = SHA256(material) as big-endian integer bytes, extended by SHA256(material || counter) as needed
```

This was ambiguous about whether the first digest block was bare `SHA256(material)` or `SHA256(material || counter)` with `counter = 0`. The hardened Engine V2 implementation always uses the latter; the spec now matches.

---

## 6. `authored_order` contract

When `optionDisplayPolicy` is `authored_order`:

- Options remain in authored `scene.decision.options[]` array order (option `id` values).
- No seed is calculated; no shuffle is performed.
- Attempt identity does not affect order.
- Stable option IDs remain submission identity.
- Server still returns and snapshots `optionDisplayOrder`.

---

## 7. `randomize_per_attempt_scene` contract

Deterministic per-attempt, per-scene shuffle:

1. Build UTF-8 seed material (§8).
2. Generate SHA-256 digest byte stream with uint32be counter suffix starting at 0 (§10–§11).
3. Fisher–Yates backward shuffle with rejection-sampled uniform index draws (§12–§13).
4. Identical inputs produce identical order; different attempt IDs may produce different orders.
5. Replay recomputes and verifies against stored order maps.
6. No Python `hash()`, no `random` module, `PYTHONHASHSEED` irrelevant.

---

## 8. Exact seed material

Field order (5 fields, newline-separated, then UTF-8 encoded):

```
attemptId + "\n" + simulationId + "\n" + version + "\n" + canonicalContentSha256 + "\n" + sceneId
```

Implementation equivalent: `"\n".join((attempt_id, simulation_id, version, canonical_content_sha256, scene_id)).encode("utf-8")`

Values used verbatim — no trimming or normalization.

---

## 9. Encoding

- **Character encoding:** UTF-8.
- **Separator:** single U+000A newline between consecutive fields.
- **No trailing newline** after the final field (`sceneId`).
- Empty strings permitted if the underlying field is empty.

---

## 10. Counter encoding

- Counter starts at **0**.
- Increments by **1** after each digest block.
- Encoded as **unsigned 32-bit big-endian bytes** (`uint32be(counter)`), 4 bytes.

---

## 11. Digest-block algorithm

For each counter value `0, 1, 2, …`:

```
block = SHA256(seedMaterialBytes || uint32be(counter))
```

Emit all 32 bytes of `block` in order, then advance counter. There is **no** separate first block of bare `SHA256(seedMaterialBytes)`.

---

## 12. Random-byte consumption

- Bytes are consumed sequentially from the concatenated stream of all digest blocks in counter order.
- **Unused bytes from a prior digest are not retained** — when a draw needs another byte, consumption continues at the next sequential byte (possibly in the next digest block).
- Rejection sampling may discard bytes (when `draw >= limit`) and request the next byte from the stream.

---

## 13. Fisher–Yates behavior

- Input: option IDs in authored document-array order.
- Copy to mutable `order` list.
- Iterate `index` from `len(order) - 1` down to `1` (inclusive).
- At each step: `swapIndex = uniform draw in [0, index]` via §17.6 rejection sampling.
- Swap `order[index]` and `order[swapIndex]`.
- Return resulting tuple/list.

**Rejection sampling (uniform index):**

- `n = upperInclusive + 1`; fail closed if `n > 256`.
- `limit = floor(256 / n) * n`.
- Read next byte `b` from stream; if `b >= limit`, discard and repeat; else return `b mod n`.

---

## 14. Golden-vector inputs

| Field | Value |
|---|---|
| `optionIds` (authored order) | `["opt-a", "opt-b", "opt-c"]` |
| `attemptId` | `golden-attempt` |
| `simulationId` | `golden-sim` |
| `version` | `1.0.0` |
| `canonicalContentSha256` | `0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef` |
| `sceneId` | `SC-GOLDEN` |
| `optionDisplayPolicy` | `randomize_per_attempt_scene` |

Source: `TestHardeningSeedGoldenVector.test_golden_vector_option_order` in `tests/test_scenario_engine_v2.py`.

---

## 15. Golden-vector expected order

```
["opt-b", "opt-c", "opt-a"]
```

---

## 16. Runtime semantics changed

**No.**

---

## 17. Engine V2 code modified

**No.**

---

## 18. Tests modified

**No.**

---

## 19. Fixtures modified

**No.**

---

## 20. Tests executed

```bash
python -m pytest tests/test_scenario_engine_v2.py tests/test_scenario_schema.py tests/test_scenario_validation_v1_1.py -q
```

---

## 21. Test results

```
185 passed in 2.53s
```

Includes `TestHardeningSeedGoldenVector.test_golden_vector_option_order` (golden vector `opt-b`, `opt-c`, `opt-a`).

---

## 22. Conflicting documents found

| Document | Issue |
|---|---|
| `SCENARIO_SCHEMA_1_1_0_SPEC.md` §17 (prior revision 5) | Ambiguous first-block SHA-256 stream |
| `SCENARIO_SCHEMA_1_1_0_CUSTOM_VALIDATION.md` §G | Incomplete counter/stream detail (not a direct contradiction, but materially incomplete for CV-103 cross-reference) |

Historical review documents (`SCENARIO_ENGINE_V2_SLICE_01_FOCUSED_REVIEW.md`, `SCENARIO_ENGINE_V2_SLICE_02_HARDENING_REPORT.md`, `SCENARIO_SCHEMA_1_1_0_ADVERSARIAL_REVIEW.md`) describe the prior ambiguity or historical findings — not updated (not normative contract; no material contradiction with frozen behavior after §17 rewrite).

---

## 23. Conflicting documents corrected

| Document | Action |
|---|---|
| `SCENARIO_SCHEMA_1_1_0_SPEC.md` | §17 rewritten (revision 6) |
| `SCENARIO_SCHEMA_1_1_0_CUSTOM_VALIDATION.md` | §G aligned to §17 revision 6 |

---

## 24. Protected paths untouched

Confirmed — no inspection, modification, or reference to:

- `.local/`
- `local_only/`
- `scripts/v58_run_combined_policy_evaluation.py`
- `structural_audit_state.json`
- `tests/test_combined_policy_evaluator.py`
- `workers/combined_policy_evaluator.py`
- `v68_corrected_review_bundle/`
- `v68_final_review_bundle/`
- `v68_review_bundle/`

---

## 25. Nothing staged, committed, pushed, or deployed

Confirmed.

---

## 26. Errors encountered

None.

---

## 27. Stop conditions encountered

None. Implementation, golden-vector test, and hardening report agree on the frozen contract.

---

## 28. Remaining risks

- Non-Python runtimes must implement identical UTF-8, uint32be, SHA-256, and rejection-sampling semantics to interoperate; §17.9 golden vector is the conformance check.
- Historical review docs still mention the old ambiguity for audit trail — readers should prefer §17 revision 6 as normative.

---

## 29. Git status

Modified (this task):

```
 M docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_CUSTOM_VALIDATION.md
 M docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_SPEC.md
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_SPEC_17_ALIGNMENT_REPORT.md
```

Nothing staged, committed, pushed, or deployed. Other untracked paths pre-existed and were not touched by this task.

---

## 30. Recommended next step

Proceed with persistence/resume integration design against the hardened Engine V2 API, using §17 revision 6 as the cross-runtime option-order contract.
