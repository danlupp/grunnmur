# 02 — Temporal model

## 2.1 Two axes, and why not more

**Valid time** (`valid`) — the period during which a norm *was in force as a matter of law*.
Norwegian: *ikrafttredelse* to *opphevelse*. This is a property of the world, not of our database.
It can be set retroactively, and it can be revised by later legislation.

**Transaction time** (`tx`) — the period during which *our database asserted* a given fact. Opens
when a snapshot taught us the fact; closes when a later snapshot contradicted it. Append-only:
transaction time is never rewritten, only extended and closed. This is what makes the database
auditable.

Together these answer the two questions that matter:

```
valid @> '2019-03-03'  AND  tx @> '2020-06-01'   -- what we believed then, about then
valid @> '2019-03-03'  AND  tx @> now()          -- what we believe now, about then
```

Three other timestamps look like axes but are **attributes**, and modelling them as axes is the
most common way these systems collapse into unqueryable complexity:

| Timestamp | Norwegian | Why it is not an axis |
| --- | --- | --- |
| Enactment | *vedtakstidspunkt* | A fact about the change act; store as a column |
| Promulgation | *kunngjøringstidspunkt* | Ditto — and it constrains, but does not define, valid time |
| Lovdata consolidation | — | We only ever learn through Lovdata, so it collapses into transaction time |

Keep two axes. Put the rest in columns.

## 2.2 The Norwegian cases that break naive designs

Each of these is a real, routine occurrence — not an exotic corner.

### Retroactivity — `valid_from < kunngjort_on`

Tax and benefit legislation is regularly given effect from 1 January of the current year while
being promulgated in June. A model that assumes valid time starts at promulgation will silently
misdate a large fraction of fiscal law. Bitemporality handles this natively; naive versioning
does not.

### `Fra den tid Kongen bestemmer`

An act is promulgated with its entry into force delegated to a later royal decree (*kgl. res.*).
At promulgation the entry-into-force date is **unknown**, not absent.

Model this as a `change_event` with `in_force_on IS NULL` and `state = 'pending'`, held in a
pending queue. When the resolution appears — typically surfaced through the footnote's
`iflg. res. <date> nr. <n>` clause or through Lovtidend — the event is resolved and the valid-time
interval is opened *retroactively in valid time but forward in transaction time*. That asymmetry
is precisely what bitemporality exists for.

### Staged entry into force

One act commonly enters into force provision by provision across months or years. Valid time
therefore has to be tracked at **provision** granularity, not document granularity. This is the
single strongest argument for the fragment-level versioning in [03](03-entity-model.md) — a
document-level model simply cannot represent "§ 5 in force, § 6 not yet" without lying.

### Rettelser (errata)

Lovdata corrects the consolidated text after the fact. The text was always legally X; our database
said Y for three weeks. Valid time is amended; transaction time preserves the record that we said
Y. Without transaction time this correction is indistinguishable from a legislative amendment —
which is a serious defect in a legal database, because one is a change in the law and the other is
a change in our knowledge of it.

### Repeal by disappearance

A repealed act stops appearing in the dump. `absence in snapshot N` closes the valid-time interval;
it must never be treated as a missing document. Guard this with the anomaly detector in
[05](05-pipeline.md#stage-7) — a bad fetch that yields a truncated tarball would otherwise repeal
half the corpus overnight.

### The reconstruction horizon

Before the first snapshot we ingest, we have change *events* (from footnotes, back decades; from
Lovtidend, back to 2001) but not the *text* those events produced. The model must say so out loud
rather than interpolating. Every provision version therefore carries:

- `text_known` — we hold the actual wording for this interval.
- `event_known` — we know a change occurred here, and when, but not the resulting text.

A point-in-time query before the horizon returns provisions with `text_known = false` and a clear
marker, never a plausible-looking guess. Guessing here is how a legal database gets someone sued.

## 2.3 Interval conventions

- **Granularity.** Valid time is a `daterange`; Norwegian entry into force is date-granular
  (`i kraft 1 jan 2006`, occasionally `straks`). Transaction time is a `tstzrange` at the
  snapshot's `fetched_at`, so every fact derived from one snapshot shares one transaction
  timestamp — which makes snapshot-level rollback trivial.
- **Closure.** Half-open `[from, to)` throughout. `i kraft 1 jan 2006` and repeal `31 des 2010`
  becomes `[2006-01-01, 2011-01-01)`.
- **Open ends.** `upper(valid) = 'infinity'` means in force with no known end.
  `upper(tx) = 'infinity'` means currently asserted — index on this, it is the hot path.
- **Non-overlap.** Enforced in the database, not in application code, via GiST exclusion
  constraints on `(entity =, valid &&, tx &&)`. See [04](04-schema.md).

## 2.4 A worked example

`§ 5-3` of an act, amended once, then corrected:

| # | valid | tx | text | note |
| --- | --- | --- | --- | --- |
| 1 | `[2006-01-01, ∞)` | `[2024-01-15, 2026-03-05)` | "A" | first ingested |
| 2 | `[2006-01-01, 2026-03-01)` | `[2026-03-05, ∞)` | "A" | closed by the amendment |
| 3 | `[2026-03-01, ∞)` | `[2026-03-05, 2026-03-20)` | "B" | amendment applied |
| 4 | `[2026-03-01, ∞)` | `[2026-03-20, ∞)` | "B′" | *rettelse*: B had a typo |

Row 1 is never deleted or altered. "What did we tell a user on 2026-03-10?" → row 3. "What was
actually in force on 2026-03-10?" → row 4. Both are answerable, forever, and the difference
between them is visible. That is the whole point.
