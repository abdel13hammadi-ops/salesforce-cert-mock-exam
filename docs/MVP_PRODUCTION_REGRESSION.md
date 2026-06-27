# CertBound MVP Production Regression Checklist

Manual production verification. Run sequentially after each release candidate.
Record pass/fail and evidence for every row.

---

## 1. New paid user — Dashboard and Daily Sprint

| Field | Detail |
|---|---|
| **Setup** | New paid test account; no prior exam attempts; preferred language `en`; one active certification assigned. |
| **Action** | Log in → open Dashboard → confirm metrics load → click Daily Sprint entry for assigned certification. |
| **Expected** | Dashboard loads without error; Daily Sprint opens Practice by Category with 10-question sprint preconfigured; app version shows `V45_MVP_LAUNCH_HARDENING`. |
| **Evidence** | Screenshot of Dashboard + Daily Sprint start screen with version caption. |
| **Pass/Fail** | |

## 2. Daily Sprint completion, save, and review

| Field | Detail |
|---|---|
| **Setup** | Continue from check 1 with Daily Sprint in progress. |
| **Action** | Answer all 10 questions → Finish Practice → review results → return to Dashboard. |
| **Expected** | One `Daily Sprint` attempt saved; score/review visible; no duplicate attempt on refresh; Dashboard Recent Attempts shows new row. |
| **Evidence** | Screenshot of completion screen + Dashboard Recent Attempts row. |
| **Pass/Fail** | |

## 3. Practice by Category — refresh recovery and completion

| Field | Detail |
|---|---|
| **Setup** | Paid user; start Practice by Category (non-sprint), answer at least 3 questions. |
| **Action** | Refresh browser mid-session → confirm answers/order restored → finish practice. |
| **Expected** | Session restores same question order and selected answers; one saved attempt after completion; recovery state cleared. |
| **Evidence** | Before/after refresh screenshots + saved attempt in My Progress. |
| **Pass/Fail** | |

## 4. Weak Areas Practice — refresh recovery and completion

| Field | Detail |
|---|---|
| **Setup** | Paid user with weak-domain data; start Weak Areas Practice, answer at least 2 questions. |
| **Action** | Refresh browser → confirm session restores → finish practice. |
| **Expected** | Domains, answers, and question order restored; one saved attempt; recovery state cleared. |
| **Evidence** | Before/after refresh screenshots. |
| **Pass/Fail** | |

## 5. Paid Mock completion and verified question-level evidence

| Field | Detail |
|---|---|
| **Setup** | Paid user eligible for full mock; no in-progress exam. |
| **Action** | Start Paid Mock → submit exam → wait for save → open My Progress verified mock section. |
| **Expected** | One parent `exam_attempts` row; question-level rows present; verified mock metrics update; no duplicate parent on refresh. |
| **Evidence** | Results screen + My Progress verified metrics screenshot. |
| **Pass/Fail** | |

## 6. My Progress verified-mock metrics

| Field | Detail |
|---|---|
| **Setup** | User with at least one full paid mock saved (check 5). |
| **Action** | Open My Progress → select same certification → inspect Verified Mock Performance. |
| **Expected** | Latest score, average, and attempt count match saved mocks only (full-length paid mocks). |
| **Evidence** | Screenshot of verified mock metrics block. |
| **Pass/Fail** | |

## 7. Readiness locked vs unlocked behavior

| Field | Detail |
|---|---|
| **Setup** | Two accounts: one below required mock count, one at/above threshold. |
| **Action** | Open My Progress on each account for same certification. |
| **Expected** | Locked account shows mocks-remaining messaging; unlocked account shows readiness score card and methodology text. |
| **Evidence** | Side-by-side screenshots of locked and unlocked states. |
| **Pass/Fail** | |

## 8. Weak-domain totals

| Field | Detail |
|---|---|
| **Setup** | Paid user with mixed domain performance from mocks/practice. |
| **Action** | Open My Progress → inspect weak-domain table/totals. |
| **Expected** | Weakest domains listed with counts/accuracy consistent with saved attempts. |
| **Evidence** | Screenshot of weak-domain section. |
| **Pass/Fail** | |

## 9. Attempt History

| Field | Detail |
|---|---|
| **Setup** | User with multiple saved attempts across modes. |
| **Action** | Open My Progress Attempt History (or equivalent section). |
| **Expected** | Attempts sorted newest-first; timestamps readable; mode/score/correct counts correct. |
| **Evidence** | Screenshot of attempt history table. |
| **Pass/Fail** | |

## 10. Multi-select limit and deselection behavior

| Field | Detail |
|---|---|
| **Setup** | Paid mock or practice question with `select_count > 1`. |
| **Action** | Select up to limit → attempt extra selection → deselect one → reselect different option. |
| **Expected** | Extra selections blocked with clear message; deselection works; final selection persists after navigation. |
| **Evidence** | Screenshot or short screen recording of limit message. |
| **Pass/Fail** | |

## 11. Logout and session timeout

| Field | Detail |
|---|---|
| **Setup** | Logged-in user on any learner page. |
| **Action** | Log out → confirm redirect/guest state; log back in → idle past session timeout threshold. |
| **Expected** | Logout clears session; timeout shows expired notice and requires re-login; no stale privileged actions. |
| **Evidence** | Screenshot of timeout notice. |
| **Pass/Fail** | |

## 12. Paid vs unpaid access restrictions

| Field | Detail |
|---|---|
| **Setup** | Free/unpaid account and paid account. |
| **Action** | Compare access to Paid Mock, Practice by Category, Weak Areas, Daily Sprint. |
| **Expected** | Unpaid sees locks/previews where designed; paid routes open; free preview mock still available to unpaid where configured. |
| **Evidence** | Screenshots of locked vs unlocked entry points. |
| **Pass/Fail** | |

## 13. Admin audit review

| Field | Detail |
|---|---|
| **Setup** | Admin account unlocked; at least one completed audit run with findings. |
| **Action** | Open Admin Audit Review → select run → open finding detail. |
| **Expected** | Runs/findings load; immutable version snapshot shown; publication gate status visible. |
| **Evidence** | Screenshot of finding detail panel. |
| **Pass/Fail** | |

## 14. Audit decision persistence

| Field | Detail |
|---|---|
| **Setup** | Open finding with status `open` from check 13. |
| **Action** | Submit `accepted` decision with required note → reload finding detail. |
| **Expected** | Decision saved; status `accepted`; decision history appended; no SQL/stack trace shown on success or failure. |
| **Evidence** | Screenshot of decision history + status. |
| **Pass/Fail** | |

## 15. Publication gate semantics

| Field | Detail |
|---|---|
| **Setup** | Version with blocking finding from check 14 (or known gated version). |
| **Action** | Inspect publication gate status → attempt manual publish (admin only). |
| **Expected** | Gate shows blocked while open/accepted blocking finding exists; publish attempt fails with readable message; no content mutation on failure. |
| **Evidence** | Screenshot of blocked gate message. |
| **Pass/Fail** | |

## 16. No duplicate submissions

| Field | Detail |
|---|---|
| **Setup** | In-progress Paid Mock or practice session ready to submit/finish. |
| **Action** | Double-click Submit/Finish → refresh results page twice. |
| **Expected** | Exactly one saved attempt; refresh does not create additional parent rows or duplicate saves. |
| **Evidence** | Database row count or My Progress attempt list showing single new row. |
| **Pass/Fail** | |

## 17. Mobile-width smoke check

| Field | Detail |
|---|---|
| **Setup** | Browser devtools ~390px width; logged-in paid user. |
| **Action** | Open Dashboard, start Daily Sprint, view one question screen, open My Progress. |
| **Expected** | Layout usable without horizontal overflow blocking primary actions. |
| **Evidence** | Mobile-width screenshots. |
| **Pass/Fail** | |

## 18. Sanitized production error behavior

| Field | Detail |
|---|---|
| **Setup** | Temporarily induce a safe failure (e.g., disconnect network during practice save retry) or use staging fault injection. |
| **Action** | Trigger save/load failure on learner page. |
| **Expected** | Short generic user message only; no SQLSTATE, traceback, file paths, credentials, or PostgREST payload in UI. |
| **Evidence** | Screenshot of user-visible error text. |
| **Pass/Fail** | |

## 19. Application version label

| Field | Detail |
|---|---|
| **Setup** | Any learner and admin page. |
| **Action** | Scroll to page footer/caption. |
| **Expected** | Label reads `V45_MVP_LAUNCH_HARDENING`; stale `V43_ACCESS_AND_READINESS_INTEGRATION` absent. |
| **Evidence** | Screenshot of version caption on Dashboard and one admin page. |
| **Pass/Fail** | |

## 20. Recent Attempts New York timezone display

| Field | Detail |
|---|---|
| **Setup** | User with at least one saved attempt having known UTC `completed_at`. |
| **Action** | Open Dashboard → Recent attempts table. |
| **Expected** | Completed timestamp shown in `America/New_York` with timezone abbreviation (EST/EDT); not raw UTC. |
| **Evidence** | Screenshot with known UTC offset verification noted in test notes. |
| **Pass/Fail** | |
