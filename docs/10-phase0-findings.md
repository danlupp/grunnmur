# 10 — Phase 0 findings (measured, not assumed)

Reconnaissance against the real data slice in [`data/`](../data), reproduced by
[`tools/recon.py`](../tools/recon.py). **Several claims in docs 01–09 were written against
Lovdata's published format documentation and are wrong about the actual dataset.** Corrections are
marked ❗ and have been applied to those documents; this page is the record of what was measured.

Corpus: 759 acts + 5 110 central regulations = **5 869 documents**, all XML, parsed with zero
failures in ~90 seconds.

## 10.1 The format is not what the documentation describes

❗ **`data-absoluteaddress` does not exist.** Zero occurrences in 5 869 documents. The ordinal-path
addressing that [docs/03 §3.2](03-entity-model.md) treats as the central identity hazard is simply
not in this dataset. What exists instead is far better:

| Attribute | Example | Role |
| --- | --- | --- |
| `data-lovdata-URL` | `SF/forskrift/2000-07-06-727/§5a` | **Stable, designator-based key** |
| `data-name` | `§5a` | Printed designator |
| `id` | `paragraf-6` | Positional anchor — unstable, informational only |
| `data-repealeddate` | `2006-07-01` | Machine-readable repeal date |
| `data-change-part` | `lov/2015-04-10-17/§20-1/ledd/1/bokstav/a` | Structured provision path |

The ordinal-instability hypothesis was still **confirmed** — in `LOV-2016-06-17-73`, `§ 5` anchors
to `paragraf-5` but `§ 5a` anchors to `paragraf-6` — it just lands on `id`, not on a key we would
have used. Provision identity now uses `data-lovdata-URL`, normalised into the `data-change-part`
idiom, so our keys and Lovdata's agree by construction instead of by derivation.

❗ **But `data-lovdata-URL` is stable, not unique.** A regulation reproducing a convention *and*
its protocol gives "Art I" in each the *same* URL (`forskrift/1977-02-15-2/aI`). 4 089 keys collide
this way. Collisions are resolved by qualifying with the nearest ancestor's own Lovdata URL
(`.../KAPITTEL_4-1/aI`), which is stable; only **26 of 756 878** provisions fall back to the
positional anchor, and those are flagged. The exclusion constraint caught this — it was not
visible by inspection.

❗ **Change annotations are `article.changesToParent`, not "footnote 0"** — 42 161 of them, in
2 869 documents — and they are *semi-structured*: the changing act is already an `<a href>`, so only
the entry-into-force clause needs grammar parsing.

❗ **A work can have parallel language expressions.** Grunnloven ships as bokmål and nynorsk under
one `legacyID` with *identical* Lovdata keys; language appears only in `<html lang>`. Language is
now part of provision identity and of the `work_state` exclusion constraint. (Lovdata writes `no`
for most documents, `nb`/`nn` for others; one document's `lang` attribute is malformed.)

## 10.2 The seven open questions, answered

| # | Question | Answer |
| --- | --- | --- |
| 1 | Is *hjemmel* machine-readable? | ✅ **Yes.** `dd.basedOn` links to the empowering provision, in 5 041/5 869 documents. 21 410 `hasLegalBasis` edges, free |
| 2 | Are future-dated provisions observable? | ✅ **Yes.** Present-tense `Endres/Tilføyes ved`, future `data-repealeddate`, "i kraft når departementet bestemmer". `not_yet_in_force` is observed, not inferred |
| 3 | Are annotations on all levels? | Yes — `changesToParent` attaches at paragraph level and deeper |
| 4 | How are annexes structured? | As ordinary `section` elements; they carry annotations too |
| 5 | Do repealed provisions remain as shells? | ✅ **Yes** — `data-repealeddate` + `(Opphevet)` heading + a repeal annotation. **Repeal is directly observable and does not depend on the absence gate**, which becomes a backstop rather than the primary signal |
| 6 | What does Lovtidend XML look like? | ❌ **Still unknown** — blocked, see §10.5 |
| 7 | Stable link from change act to target? | ✅ **Yes** — every annotation carries the changing act as an href |

## 10.3 Change extraction, measured

92 536 change events from 42 161 annotations (annotations routinely list several sources).

| Metric | Result |
| --- | --- |
| Events naming a changing act | **99.8 %** |
| Entry into force stated explicitly | 63.8 % |
| Explicitly deferred (`Kongen bestemmer`, `straks`, `virkningstidspunkt`) | 1.2 % |
| Lower bound only (act named, no date stated) | 34.8 % |
| **No temporal information at all** | **1 event of 92 536** |

Operations: 68 370 amend, 15 509 insert, 5 184 renumber, 3 473 repeal.

Three forms the plan did not anticipate, all now handled:

- **Nynorsk verbs** — `Endra`, `oppheva` (868 annotations).
- **Mixed operations in one annotation** — `Tilføyd ved forskrift X, opphevet ved forskrift Y` is an
  insert *and* a repeal. Taking the leading verb for the whole annotation under-counted repeals by
  72 %; the verb is now read per source.
- **Effect clauses instead of entry into force** — `(fom inntektsåret 2005)`, `(med virkning for
  regnskapsår …)`. Fiscal law states valid time in a different vocabulary; 2 118 recovered.

### ❗ The changing act's own date is NOT a usable fallback

For the 34.8 % with no stated date, the obvious move is to use the changing act's date — it is
encoded in its identifier. Measured against the 59 050 events that *do* state a date:

| | lag = entry into force − changing act's own date |
| --- | --- |
| same day | **1.9 %** |
| median | **22 days** |
| p75 / p95 / p99 | 124 / 560 / 1 193 days |
| negative (**retroactive**) | **2.9 %** |

So that inference would be wrong far more often than right, and the 2.9 % negative tail confirms
the retroactivity case in [docs/02](02-temporal-model.md) empirically. It is therefore stored as
`change_event.in_force_earliest` — a **lower bound to query with, never a date to display**.

(One caveat, stated rather than hidden: this is measured over annotations that state a date, and
those may state it *because* it differs from the act date. The bound is sound either way; the
selection effect only means the true same-day share is probably higher than 1.9 %.)

## 10.4 What the loaded database contains

5 869 documents → **756 878 provisions**, 92 536 change events, 25 185 edges, 1 116 MB.

- **Text deduplicates 29.6 %** by content hash (533 121 distinct blobs), confirming the
  content-addressed storage decision in [docs/04](04-schema.md).
- **Works in force** rises 784 (1990) → 1 453 (2000) → 2 738 (2010) → 4 232 (2020) → 5 844 (today).
- 99.7 % of change events link to a specific provision.
- 3 052 events are pending entry into force.
- The orphaned-regulation query finds **83 regulations** whose *hjemmel* is no longer in force.

### Reconstructed intervals

The dump holds only current text, so a provision amended in 2015 has one version valid from 2015.
Queried at 2010 it would return *nothing* — indistinguishable from "did not exist".
[`schema/020_reconstruct.sql`](../schema/020_reconstruct.sql) fills those gaps from change-event
dates with **46 865 rows carrying `text_known = false`**, so the query answers "in force, wording
not held". Wording held, by date: 97.7 % (2000), 97.6 % (2010), 98.5 % (2020), 100 % (today).

## 10.5 Norsk Lovtidend — ingested

Initially blocked (`api.lovdata.no`, `lovdata.no` and `data.norge.no` are all refused by this
environment's egress policy, 403 on CONNECT), then supplied directly as two archives:
**39 157 documents**, 2001 → current year, under `lti/<year>/`.

Same markup vocabulary as the consolidated slice, with two differences that mattered:

- ❗ **`Endrer` and `Hjemmel` refs are bare text, not `<a href>`** (`<li>forskrift/2004-12-17-1852</li>`).
  Extracting only links yielded **zero** edges from 39 157 documents. With the text fallback:
  48 487 `amends` and 138 345 `hasLegalBasis` edges.
- `journalNumber` is present; `dokid` begins `LTI/`, which is what distinguishes a promulgated
  document from a consolidated one and lets change acts be typed as
  `endringslov` / `endringsforskrift`.

❗ **A work can exist in both collections** — promulgated in Lovtidend, then consolidated. Those are
two expressions of one work with overlapping valid time, and the `work_state` exclusion constraint
rejected the load until the consolidated expression was given precedence. The Lovtidend
promulgation date survives as `work.published_on`.

### What it changed

| | consolidated only | with Lovtidend |
| --- | --- | --- |
| Real works | 5 868 | **40 748** |
| Stub works (cited, no body) | 20 138 | **5 113** (−75 %) |
| Change events resolving to a *known* act | **10.5 %** | **92.8 %** |
| `amends` edges | 3 775 | 52 262 |
| Provisions | 756 878 | 2 165 352 |
| Text deduplication | 29.6 % | 49.4 % |
| Database | 1.1 GB | 2.7 GB |

30 160 of the works are change acts — the documents that were previously invisible.

### ⏭ Not yet done: operative text extraction

Change acts carry the actual amending text, in the form
[docs/06 §6.2](06-change-extraction.md#62-lovtidend-change-acts) anticipated:

```
I lov 6. juni 1975 nr. 29 om eigedomsskatt til kommunane gjøres følgende endringer:
§ 24 skal lyde:
§ 24. Eigedomsskatten skal svarast til den kommunen der skatten er utskriven.
§ 25 annet ledd skal lyde:
Kommunen kan i særlege høve gjeva utsetjing.
```

**72.5 % of Lovtidend documents** contain such instructions (`skal lyde` / `oppheves` / `blir ny`,
measured over a 3 000-document sample). Parsing them into `(target provision, new text,
entry into force)` triples is what converts the 46 883 `text_known = false` intervals into real
historical wording — the difference between knowing *that* a provision changed and knowing what it
said. The data is now in hand; the extractor is not yet written, and is the single highest-value
next piece of work.

One structural wrinkle to plan for: change acts are divided into roman-numeral parts (`I`, `II`,
`III`), and different parts can carry different entry-into-force dates, so the operative parser
must attach dates per part rather than per act.
