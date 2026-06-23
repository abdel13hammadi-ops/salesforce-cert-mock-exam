# V44 Question-Version Foundation — Compatibility Reference

## What changes in V44

V44 adds three new tables to the `public` schema. No existing table is altered.
All changes are purely additive.

## Stable live identity tables (unchanged)

| Table | Role |
|---|---|
| `public.questions` | Stable live identity — the canonical question record. Its `id` column is the FK target for all version tables. No column is added, removed, or renamed. |
| `public.answer_options` | Live option table used by the current exam delivery path. Unchanged. |

## New additive tables

| Table | Purpose |
|---|---|
| `question_versions` | Immutable content snapshot for each version of a question. Each row captures `question_text`, `explanation`, options metadata, `content_hash`, and provenance at a point in time. |
| `question_option_versions` | Immutable option snapshots belonging to a `question_versions` row. Cascades on version delete (version delete is not expected in normal operations). |
| `question_version_events` | Full audit trail of version lifecycle events: created, submitted_for_review, approved, rejected, published, superseded, override_applied, deactivated. |

## Current exam delivery — unaffected

- The exam delivery path in `app.py` continues to read from `public.questions` and `public.answer_options` exactly as before.
- `question_attempts` rows continue to reference the live `questions.id`. No column is added to `question_attempts` in this phase.
- `question_attempts.question_content_version` (an integer field capturing the `content_version` at attempt time) remains compatible. It records the human-assigned `content_version` from the import payload, not a FK to `question_versions`. These two numbering systems are independent until a future reconciliation migration aligns them.

## Admin Import — unchanged

`pages/Admin_Import.py` and the `admin_import_questions_batch_v1` RPC are unchanged. The RPC continues to write directly to `public.questions` and `public.answer_options`. Integration with `question_versions` (recording a version row when a question is imported or updated) is deferred to a later phase.

## Admin Question Review — unchanged

`pages/Admin_Question_Review.py` reads from `public.questions` and `public.answer_options` and is unchanged in this phase.

## Content Pipeline candidates

Generated question candidates will remain outside the live `questions` table until they are approved. The intended flow is:

```
candidate created
  → question_versions row (source_type = 'generated', no questions.id FK yet)
  → submitted_for_review event
  → approved event
  → published: a new public.questions row is inserted and the version is linked
  → question_version_events: 'published' event records the new questions.id
```

This publish step is not implemented in V44. V44 only establishes the schema foundation.

## FK type blocker

The `question_id` FK columns in `question_versions` and `question_version_events` reference `public.questions(id)`. The exact type of `questions.id` (`integer` or `bigint`) is not determinable from repository evidence alone — no DDL schema file exists. The migration is marked `BLOCKED` until this is confirmed.

See `supabase/README.md` for the query to resolve this.

## RLS note

RLS is enabled on all three new tables. Service-role access bypasses RLS. No anon or authenticated write policies are added in this phase. Pipeline and admin policies will be added in a later migration.
