# grunnmur

A bitemporal database of Norwegian law — every *lov* and *sentral forskrift*, reconstructed so
that any point in time can be queried along **two** axes:

> "What did the law say **on 3 March 2019** (valid time), **as far as we knew on 1 June 2020**
> (transaction time)?"

The second axis is not academic. Lovdata's open dataset ships only the *current* consolidated
text, corrections (*rettelser*) rewrite the past, and consolidation lags promulgation. A database
that stores only "what the law says now" cannot answer why it told you something different last
year, and cannot be audited. `grunnmur` is designed so that every answer is traceable back to the
sha256 of a specific file fetched on a specific night.

## Status

Planning. This repository currently contains the design only — no code yet. Start at
[`docs/01-sources.md`](docs/01-sources.md) and read forward.

## The plan

| Doc | Contents |
| --- | --- |
| [01 — Sources](docs/01-sources.md) | What each upstream API gives us, and — more importantly — what it does not |
| [02 — Temporal model](docs/02-temporal-model.md) | The two axes, why not three, and the Norwegian edge cases that break naive designs |
| [03 — Entity model](docs/03-entity-model.md) | Work/Expression/Provision layering, stable identity, and the relationship taxonomy |
| [04 — Schema](docs/04-schema.md) | Concrete PostgreSQL DDL with bitemporal exclusion constraints |
| [05 — Pipeline](docs/05-pipeline.md) | The eight nightly stages, idempotency rules, orchestration |
| [06 — Change extraction](docs/06-change-extraction.md) | Footnote-0 grammar, snapshot diffing, and how the two are reconciled |
| [07 — Query surface](docs/07-query-surface.md) | `as-of` queries, document assembly, the API |
| [08 — Roadmap](docs/08-roadmap.md) | Six phases with acceptance criteria |
| [09 — Risks](docs/09-risks.md) | Pitfalls, legal caveats, open questions |

The DDL and query functions in docs 04 and 07 are also shipped as runnable files under
[`schema/`](schema/), verified against PostgreSQL 16 — see [Verification](#verification) below.

## The shape of the answer, in one page

Three facts drive the whole design:

1. **Lovdata's open dump is a snapshot, not a history.** It contains current law only — no
   repealed acts, no historical versions. Valid-time history therefore has to be *reconstructed*,
   not downloaded.
2. **The consolidated text carries its own provenance.** Every paragraph has a footnote 0 reading
   roughly `Endret ved lov 17 juni 2005 nr. 62 (i kraft 1 jan 2006 iflg. res. 17 juni 2005
   nr. 603)`. These annotations are formulaic, machine-parseable, and reach back decades — further
   than any other free source.
3. **Nightly snapshots are the ground truth going forward.** Diffing snapshot N against N−1 tells
   you exactly what changed and bounds when we learned it. Retain every distinct snapshot forever
   (they are small) and transaction time becomes fully reproducible rather than merely recorded.

So: **footnotes and Lovtidend supply valid time; snapshots supply transaction time; the diff is
the referee that proves the other two are complete.** Every text change must be explained by at
least one extracted change event — an unexplained change blocks promotion and lands in a review
queue. That single invariant is what turns a scraper into a database you can testify from.

## Verification

The design's central claim is that the store enforces bitemporal correctness in the database rather
than in application code. That claim is tested, not asserted:

```
psql -f schema/001_init.sql -f schema/010_functions.sql
psql -f schema/test_bitemporal.sql
```

Six cases run against a scratch PostgreSQL 16 cluster, all passing: overlap in both axes is
rejected by the exclusion constraint; overlap in *either axis alone* is accepted (both are normal —
a *rettelse* overlaps in valid time, staged entry into force overlaps in transaction time); deletes
are silently dropped by the append-only rule; and one valid date queried at two different `known`
timestamps returns two different texts, which is the whole point of the exercise.

## Licence and provenance

Upstream data is [NLOD 2.0](https://data.norge.no/nlod/no/2.0) (Lovdata) and requires attribution.
A reconstruction is **not** an authoritative legal source; see [docs/09-risks.md](docs/09-risks.md).
