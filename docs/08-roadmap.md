# 08 — Roadmap

Six phases. Each has an acceptance criterion that is a *demonstration*, not a checkbox — if you
cannot show the thing working, the phase is not done.

> **Where this actually stands.** The user supplied a data slice directly, so the work ran
> Phase 0 → 3 → 4 rather than in order: the historical database is built and queryable, while the
> nightly fetch (Phase 1) and snapshot diffing (Phase 2) are still to do. That inverts the ordering
> principle below, which remains the right advice for the *nightly* pipeline.

Ordering principle: get the transaction-time spine correct and boringly reliable before attempting
any valid-time reconstruction. Snapshots accumulate value from the day you start collecting them
and cannot be back-collected; reconstruction can be redone at leisure. **If you do nothing else
this month, start the nightly fetch.**

---

## Phase 0 — Reconnaissance ✅ **done** — [findings](10-phase0-findings.md)

Confirm the assumptions this plan rests on, against real bytes.

- Download both tarballs and the Lovtidend ZIPs by hand; unpack; read a dozen documents.
- Inventory the actual element and attribute vocabulary. Verify `data-absoluteaddress` semantics
  and, specifically, **confirm the ordinal-instability hypothesis** in
  [03 §3.2](03-entity-model.md#32-identity--the-trap-worth-ten-pages) by finding an act with an
  inserted `§ Xa`.
- Collect 500 footnote-0 strings and hand-classify their forms.
- Check whether the *hjemmel* is machine-readable or prose.

**Accept when:** a one-page findings note lists every correction needed to
[01](01-sources.md) and [03](03-entity-model.md). Expect corrections to names and details, not to
the architecture. Resolve the egress block on `api.lovdata.no` first — it is the only hard blocker
in the plan.

## Phase 1 — Snapshot spine — *parse done, nightly fetch not started*

Stages 0–2. No temporal reasoning at all.

- `grunnmur fetch` — content-addressed raw store, conditional requests, retries, `snapshot` rows.
- `grunnmur parse` — parsed layer as Parquet: metadata, structure, text, footnotes, links.
- Nightly cron. Alerting on failure.

**Accept when:** the job has run unattended for seven consecutive nights; a 304 produces an
observation and no new content rows; re-running any night is a verified no-op.

## Phase 2 — Identity and diff — *identity done, diff not started*

Stages 3–4 — the technical heart of the project.

- The `logical_key` normaliser, as one shared module with an exhaustive test suite.
- Canonicalisation with a versioned `canon_version`.
- Three-pass tree diff with move/renumber detection and confidence scores.
- The absence gate and corpus-wide anomaly detector.

**Accept when:** replaying two snapshots that straddle a known amendment produces exactly the
provisions that actually changed and nothing else. The phantom-diff rate from formatting churn is
the metric that decides whether canonicalisation is finished — target zero.

## Phase 3 — Change extraction ✅ **`changesToParent` done** (99.8 %); Lovtidend ingested, operative text pending

Stage 5 — [06](06-change-extraction.md).

- Footnote-0 PEG grammar; unparsed-residue queue.
- Lovtidend operation and provision-reference grammars.
- Reference extraction into `edge`; short-title lexicon.
- LLM fallback, clearly marked and confidence-capped.

**Accept when:** footnote parse rate exceeds 99 % across the whole corpus, and the residue is
triaged rather than merely counted.

## Phase 4 — Bitemporal store ✅ **loaded and queryable**

Stages 6–8 and the schema in [04](04-schema.md).

- Full DDL with exclusion constraints and the append-only rule.
- Reconciliation truth table; conflict and pending queues.
- Validation gates, including the completeness invariant.
- A minimal review UI. It need not be pretty; it needs to exist, or the queues silently become a
  landfill and the automation claim becomes false.

**Accept when:** ❗ **the worked example in [02 §2.4](02-temporal-model.md#24-a-worked-example)
round-trips.** Take a real act amended during the collection period, and answer both "what did it
say on date D" and "what did we think on date T" correctly, with every answer traceable to a
snapshot sha256. This is the phase that makes the database bitemporal rather than merely versioned.

## Phase 5 — Backfill and enrichment (4–6 weeks)

- ✅ Lovtidend 2001→ ingested (39 157 change acts) and all change annotations extracted.
- ✅ Operative instructions parsed (74 149 amendments) and loaded as 16 052 historical text
  versions — see [docs/11](11-amendment-extraction.md).
- ✅ Repeals and renumbers applied as temporal operations (conservatively — see
  [docs/11 §11.6](11-amendment-extraction.md)), plus 12 381 provisions recovered that no longer
  exist in current law.
- ✅ Historical *structure* recovered — not by replaying from promulgated text (Lovtidend starts in
  2001, so most originals do not exist) but by reading the ledd/punkt structure carried inside each
  whole-paragraph replacement. Exact attachments 32 205 → 67 617; recovered provisions 12 381 →
  30 677, 83 % with a parent.
- ⏭ Place the 21 245 paragraph-level attachments that replacement text cannot reach; triage the 971
  past repeals on provisions still present.
- Populate `text_known = false` intervals honestly rather than interpolating.
- Stortinget enrichment: `enactedBy` edges.
- Query API ([07](07-query-surface.md)).

**Accept when:** a point-in-time query for a date in 2010 returns a defensible answer, with
unknown-text provisions clearly flagged as such rather than omitted or invented.

## Phase 6 — Products (ongoing)

Everything the model gives away nearly free once phases 1–5 hold:

- Nightly change bulletin; feeds per work or per ministry.
- The orphaned-regulation report ([07 §7.5](07-query-surface.md#75-graph-queries-the-model-makes-cheap)).
- Consolidation-lag dashboard — the window where other databases are quietly wrong.
- Akoma Ntoso and ELI export; bulk Parquet dumps.
- ✅ Citation graph built — 172 754 references, see [docs/12](12-citation-graph.md).
- Citation-graph analytics beyond impact analysis.

---

## Sequencing risks

- **Phase 2 is the one to over-invest in.** Identity mistakes propagate into every later table and
  are expensive to unwind once transaction history has accumulated on top of them.
- **Do not let the review queue become optional.** An automated pipeline with an unattended queue
  is a manual pipeline with extra steps.
- **Resist starting at Phase 5.** Reconstructing 2001–present is the interesting problem and the
  wrong one to begin with; without phases 1–4 there is nothing to check the reconstruction against.
