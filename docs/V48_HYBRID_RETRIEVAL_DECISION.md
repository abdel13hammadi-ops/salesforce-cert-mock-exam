# V48 Hybrid Retrieval Investigation — No-Go Decision Record

**Status:** Closed (no-go for production)  
**Date:** 2026-07-04  
**Scope:** CertBound V48 hybrid retrieval experiment (`hybrid_question_match_v2` evidence path only)

## Problem investigated

CertBound uses `bm25_question_match_v1` for structural evidence retrieval during AI quality audits. V48 tested whether adding OpenAI text embeddings and cosine similarity on top of the existing BM25 candidate pool could:

1. Support a semantic qualification threshold (`qualified_v2`), or
2. Rerank BM25 candidates using semantic similarity as an automatic signal.

The investigation was limited to a controlled offline/real-evidence replay of the frozen 10-question V48 fixture, with human relevance labels on the scored pairs.

## Architecture tested

- **Stage 1 (unchanged):** `bm25_question_match_v1` structural classification and candidate selection.
- **Stage 2 (experimental):** Authoritative query and chunk text resolution, durable Supabase embedding cache reads only, cosine similarity scoring via `text-embedding-3-small` (`openai-text-embedding-3-small-2026-07-03`, 1536 dimensions).
- **Evidence collection:** Cache-only hybrid replay runner and local relevance review packet workflow. No live worker integration, no shadow-evaluation writes, no production audit mutation.

Infrastructure for authoritative text resolution, cache-backed replay, local review packets, and label sidecars was implemented and validated. The semantic signal itself did not demonstrate sufficient value.

## Evidence identities

| Artifact | SHA-256 hash |
|---|---|
| Replay content set | `b7c05c1c04b2b55e37919990408068c6df244db41a28970d283821ce4f3d61e3` |
| Source review packet | `a2106b8a4719349392ec19682196145506206ab8ef2138e988469041a8686942` |
| Finalized label set | `827e108122ddde5b44d38972e85818fdda4063999753d3008dd2cbd48c702e76` |

Local artifacts tied to these hashes remain under `.local/v48/relevance_review/` and are Git-ignored. They were not committed.

## Sample size and limitations

| Metric | Value |
|---|---|
| Frozen questions | 10 |
| Semantic-review questions | 7 |
| Scored question–candidate pairs | 14 |
| Human labels — relevant | 2 |
| Human labels — partially relevant | 3 |
| Human labels — irrelevant | 9 |

**Limitations:** Small labeled sample, single embedding model/configuration, single chunking source, and exam-guide-heavy candidates in several pairs. Results are sufficient to reject this specific configuration for production; they are not a general proof about embeddings in all retrieval contexts.

## Threshold results

Cosine similarity did not separate human-labeled relevance classes:

| Metric | Score |
|---|---|
| Highest irrelevant similarity | 0.561104670 |
| Highest relevant similarity | 0.451049223 |
| Relevant mean | 0.431801501 |
| Irrelevant mean | 0.445485023 |

Irrelevant pairs scored **higher on average** than relevant pairs, and the highest irrelevant score exceeded the highest relevant score. **No defensible global semantic threshold** exists for qualification on this dataset and configuration.

## Pairwise reranking results

Among the 7 semantic-review questions, pairwise comparison of the two top BM25-selected candidates per question:

| Outcome | Questions |
|---|---|
| Higher semantic score ranked better (correct) | 1 |
| Higher semantic score ranked worse (incorrect) | 2 |
| Same relevance class (reranking inconclusive) | 4 |

Among the **3 questions where the two candidates had different human relevance labels**, semantic ranking was correct once and wrong twice. The tested score is unsuitable as an automatic reranking signal.

## Final decision

**No-go for production integration of the tested hybrid configuration.**

| Action | Decision |
|---|---|
| Implement semantic-only `qualified_v2` | **Do not** |
| Add cosine-similarity qualification threshold | **Do not** |
| Use tested score for automatic candidate reranking | **Do not** |
| Change `bm25_question_match_v1` | **Do not** |
| Integrate hybrid modules into live worker | **Do not** |
| Preserve V48 offline/replay tooling | **Yes** |
| Close V48 investigation | **Yes — completed with no-go outcome** |

This decision does **not** claim that embeddings are universally ineffective. It only rejects **this tested configuration** (query construction, chunk source, model version, segmentation, and sample) for CertBound production use.

## Production implications

- Production audit retrieval continues to use **`bm25_question_match_v1` only**.
- No semantic cutoff, qualified_v2 rule, or hybrid reranker is added to live audit passes.
- Durable embedding cache rows populated during evidence replay remain available for future experiments but are not read by production audit workers.
- No migration, RLS, worker wiring, job-queue, or shadow-evaluation changes are required to enforce this decision.

## Preserved tooling

The following remain in the repository for future offline experiments:

- Real hybrid replay runner (`scripts/v48_real_hybrid_replay.py`)
- Authoritative embedding text resolution (`workers/v48_hybrid_replay_authoritative_text.py`)
- Cache-only relevance review packet builder (`scripts/v48_build_relevance_review_packet.py`)
- Local label sidecar workflow (`scripts/v48_manage_relevance_labels.py`)
- Offline tests and frozen V1 replay fixtures (unchanged)

## Conditions to reopen the experiment

Reopen only if materially new evidence addresses the failure modes above, such as:

- **Different query construction** (e.g., stem-only vs. stem+domain, option-aware variants with controlled ablation).
- **Different embedding model** or model version with documented improvement on held-out labels.
- **Better chunk segmentation** or resource selection so candidates contain answerable content rather than exam-outline taxonomy.
- **Substantially larger human-labeled dataset** (more questions, more pairs per question, multiple cert domains) with inter-rater agreement.
- **Demonstrated improvement on held-out labels** — defensible threshold or reranking accuracy on data not used to tune the approach.

Any reopening should repeat the same evidence discipline: authoritative text resolution, cache-only or explicitly bounded provider use, human-labeled ground truth, and a written go/no-go record before live worker integration.
