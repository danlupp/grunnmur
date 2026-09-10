# 05 — Pipeline

Eight stages, run nightly. Every stage is idempotent and snapshot-scoped: re-running it with the
same input produces the same output and is safe. Data moves through three immutable layers —
**raw** (bytes as fetched), **parsed** (structured, no interpretation), **curated** (the bitemporal
database) — and never flows backwards.

```
  0 acquire ─→ 1 register ─→ 2 parse ─→ 3 canonicalise ─→ 4 diff
                                                            │
  7 validate ←─ 6 assemble ←─ 5 extract events ←────────────┘
       │
       └─→ 8 promote
```

## Stage 0 — Acquire

Fetch the two Lovdata tarballs, the Lovtidend current-year ZIP, and (weekly) the Lovtidend annual
archive. Send `If-None-Match` / `If-Modified-Since`; a 304 still records an *observation* — knowing
the corpus was unchanged on a given night is itself a fact worth storing, and it is what lets you
distinguish "nothing changed" from "we did not look".

Write bytes straight to content-addressed storage (`raw/<sha256>`), never to a mutable path.
Retry with exponential backoff; a failed night is a gap, not a corpus change, and must never reach
stage 4.

Schedule around 05:00 Europe/Oslo — Lovdata rebuilds packages overnight.

## Stage 1 — Register

Insert a `snapshot` row. If `content_sha256` equals the previous run's for the same source, mark
the observation and stop: no new content, no diff, no events. This makes the whole pipeline a
no-op on quiet nights, which is most nights for `gjeldende-lover`.

`fetched_at` becomes the transaction timestamp for everything downstream. Pin it once, here.

## Stage 2 — Parse

Unpack; parse each document with `lxml` in recovery mode (the payload is XML-compatible *HTML*, so
expect entities and stray markup). Extract into the parsed layer as Parquet or newline JSON:

- **Document metadata** — id, doc_type, date, number, title, short title, ministry, *hjemmel*.
- **Structure tree** — one record per element: `absoluteaddress`, level, designator, heading,
  `ordinal`, parent address.
- **Text** — inner markup preserved verbatim (`xml`) alongside extracted plain text.
- **Footnotes** — especially footnote 0 per paragraph, raw and unparsed at this stage.
- **Links** — `href`/`data-*` reference targets, raw.

Rule: **stage 2 interprets nothing.** It is a faithful transcription. Every judgement call happens
downstream where it can carry an `evidence` row.

## Stage 3 — Canonicalise

Compute `logical_key` per element ([03 §3.2](03-entity-model.md#32-identity--the-trap-worth-ten-pages))
and a stable content hash. Canonicalisation must be deterministic and versioned:

- Unicode NFC; strip soft hyphens and zero-width characters.
- Non-breaking spaces → spaces; collapse runs of whitespace; strip leading/trailing.
- Normalise quotation marks and dash variants.
- Resolve character entities.
- **Exclude** presentational attributes and `absoluteaddress` from the hash.

Without this, formatting churn in Lovdata's renderer produces thousands of phantom amendments and
the whole reconciliation invariant collapses. Store `canon_version` alongside each hash: changing
the algorithm later requires a full recompute, and you need to know which rows predate it.

## Stage 4 — Diff

Tree diff of snapshot N against N−1, matching in three passes:

1. **By `logical_key`** — the common case, near-total coverage.
2. **By content hash** for unmatched nodes — catches moves and renumbering.
3. **By similarity** (token-level, e.g. ≥ 0.85 Jaccard or normalised edit distance) for the
   remainder — proposes `renumber`/`move` with a confidence score.

Output per document: `added | removed | modified | moved | renumbered`, each with before/after
hashes and a confidence.

### Absence handling {#stage-4}

A work present in N−1 and absent in N is **evidence of repeal** — but only if the snapshot is
sound. Gate it: if more than a configured fraction of the corpus (start at 1 %) disappears in one
night, halt the run and alert. A truncated download must never be able to repeal Norwegian law.

## Stage 5 — Extract change events

Turn footnote-0 annotations and Lovtidend change acts into `change_event` rows. This is the
subtlest stage and has its own document: [06 — Change extraction](06-change-extraction.md).

## Stage 6 — Assemble temporal intervals

Fuse the evidence streams into `provision_version` rows. Precedence is fixed and not negotiable
per-case:

| Question | Authority |
| --- | --- |
| **When** did it take effect (valid time)? | Lovtidend > footnote 0 > diff-inferred |
| **When** did we learn it (transaction time)? | The snapshot pair, always |
| **What** does it now say (text)? | The snapshot, always |

Where a footnote says "i kraft 1 jan 2026" and the diff first sees the change on 2026-03-05, that
is **normal** — Lovdata consolidated late. Valid time opens 2026-01-01; transaction time opens
2026-03-05. The gap is real information, not an error, and is exactly what a single-axis database
throws away.

Where sources genuinely contradict — two different `i kraft` dates for one provision — write
`change_event.state = 'conflict'` and **do not** pick a winner. Conflicts block promotion of the
affected work only, not the whole run.

## Stage 7 — Validate {#stage-7}

Promotion gates. Any failure blocks stage 8 for the affected scope.

- **Schema** — every parsed document matches the expected element inventory. New or unexpected
  elements are a warning and a review item, since Lovdata's format will drift.
- **Referential integrity** — every `hasLegalBasis` target resolves, or is recorded as unresolved.
- ❗ **The completeness invariant** — *every text change detected in stage 4 must be explained by at
  least one `change_event` from stage 5.* Unexplained changes go to the review queue and block
  promotion of that work. This single rule is what separates a database you can testify from and a
  scraper with a nice schema.
- **Temporal integrity** — no overlapping intervals (the DB enforces this, but assert it in tests
  so failures surface as a clear message rather than a constraint violation); no valid interval
  opening before the work's enactment except where `retroactive` is set.
- **Anomaly detection** — corpus-wide change rate, document count delta, mean document length
  delta, all against a rolling baseline. Halt on outliers.

## Stage 8 — Promote

In one transaction: close outgoing `tx` intervals, insert new rows, mark the snapshot `promoted`.
Publish read models (see [07](07-query-surface.md)). Emit a change bulletin — "these works changed,
these entered into force, these are pending a royal decree" — which is a genuinely useful product
in its own right and costs nothing extra.

## Orchestration

Deliberately unglamorous for v1: a single `grunnmur` CLI with one subcommand per stage, each taking
`--snapshot-id`, driven by cron or a systemd timer. `grunnmur run --all` chains them.

That gives you idempotency and resumability without an orchestrator to operate. Move to Prefect or
Dagster when you actually want backfill fan-out and a run UI — around Phase 4, not before.

Suggested stack: Python 3.12, `lxml` for parsing, `pydantic` for the parsed-layer contracts,
`psycopg` + plain SQL migrations (`sqlc`-style, or Alembic), Parquet for the parsed layer, and
DuckDB for ad-hoc analysis over it.

### Backfill mode

The first run is different and should be treated as its own program: parse the full Lovtidend
archive 2001→ and every footnote-0 annotation in the current corpus, build the change-event
history, and reconstruct as much valid time as the evidence supports — accepting that pre-horizon
provisions land with `text_known = false`. Run it once, offline, and inspect it by hand before
switching to incremental. Budget real time for this; it is where the domain surprises live.

### Retention {#retention}

Keep **every distinct raw snapshot forever**. Compressed the corpus is tens of megabytes, and
content-addressing means unchanged nights cost nothing. Even a naive worst case is ~20 GB/year —
trivial against the value of being able to re-derive any historical answer from bytes rather than
from trust. This is the cheapest and most important operational decision in the project.
