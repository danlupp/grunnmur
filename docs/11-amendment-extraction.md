# 11 — Amendment extraction

Turning Norsk Lovtidend change acts into **historical wording**. The
`changesToParent` annotations in the consolidated text give change *dates*; only the change acts
themselves carry the *text*, and this is what recovers it.

Implemented in [`tools/amendments.py`](../tools/amendments.py) (extract) and
[`tools/load_amendments.py`](../tools/load_amendments.py) (assemble and load).

## 11.1 What a change act looks like

```
<section class="section" id="kapittel-1">
  <h2>I</h2>                                          ← roman-numeral part
  <article class="legalP">I lov 6. juni 1975 nr. 29 … gjøres følgende endringer:</article>
  <article class="defaultP">§ 24 skal lyde:</article> ← instruction
  <article class="futureLegalArticle">§ 24. Eigedomsskatten skal svarast …</article>  ← new wording
  <article class="defaultP">§ 25 annet ledd skal lyde:</article>
  <article class="legalP">Kommunen kan i særlege høve gjeva utsetjing.</article>
</section>
```

Only **329 of 39 157** documents carry the structured `data-change-part` attributes, so this is a
prose-parsing job. Measured over a 4 000-document sample, the instruction vocabulary is dominated by
one shape: `§X [subdivision] skal lyde:` — `skal lyde` is 88 % of all operative verbs, with
`oppheves` (770), `blir ny/nytt/nye` (400) and `utgår` (45) making up most of the rest.

## 11.2 Three things that cost a rewrite

**Verb anchoring differs per verb.** Anchoring every verb to end-of-line found `skal lyde` but
missed `§ 4 oppheves.` (trailing period) and `§ 3 blir ny § 4` (verb mid-line, with the new
designator after it). That yielded **20 repeals in the whole corpus** where the sample predicted
thousands. Each verb now gets its own anchoring; repeals went 20 → 5 190 and renumbers 0 → 2 472.

**Instructions nest.** Scanning only a part's direct children missed 16 % of acts, where
instructions sit inside lists, tables or subsections. The parser now walks the part in document
order and recovers the wording by splitting the part's text at the instruction boundaries, which
works at any depth.

**Not every target is a §.** `Vedlegg A, raden for Sverige … skal lyde:`,
`Innledningsteksten skal lyde:`, `EØS-henvisningsfeltet skal lyde:` are real amendments to annexes,
headings and EEA reference fields. They are recorded with `target_kind = 'other'` rather than
dropped — 6 986 of them.

## 11.3 Results

**74 149 amendments** from 19 364 acts (49 % of the corpus; the rest are entry-into-force
resolutions, delegations and similar). 93 % carry an entry-into-force date.

| Operation | Count |
| --- | --- |
| amend | 53 719 |
| insert | 12 768 |
| repeal | 5 190 |
| renumber | 2 472 |

### ❗ Sub-paragraph keys are historical, so resolution is two-level

40 182 amendments resolve to a provision — 32 205 on the full key, **7 977 only at § level**. That
second number is not sloppiness, and collapsing it into the first would be wrong:

> `§ 5 andre ledd` names the ledd structure **as it stood on the amendment's date**. Ledd are
> ordinal, so a 2005 amendment's "andre ledd" need not be today's second ledd.

Attaching at § level with `sub_target` recorded states exactly what is known. Of the 30 339 that do
not resolve at all, 8 992 target works that are stubs (acts since repealed, absent from current law)
and ~11 500 target paragraphs that no longer exist — both inherent to a current-law corpus, not
parser defects.

### ❗ Amendments tighten the valid time of the current text

An amendment dated *after* what we believed was the current text's start is evidence that the
belief was wrong — `cur_start` came from `changesToParent`, which is only a lower bound for the
34.8 % of events stating no date. Reconciling the two (docs/05 stage 6, on rows not yet published)
cut amendments stranded after the current text from **20 089 to 77**, and raised historical
intervals from 10 720 to **16 052**.

## 11.4 Effect on the database

| | before | after |
| --- | --- | --- |
| Versions with real wording | 2 165 352 | 2 181 404 |
| — of which historical (Lovtidend) | 0 | **16 052** |
| Reconstructed, wording unknown | 46 883 | **34 428** |
| Provisions with more than one wording | 0 | **9 584** |

Wording held, by valid date: **99.02 %** (2005), 99.31 % (2010), 99.44 % (2015), 99.63 % (2020),
100 % (today).

A provision now reads as a genuine chain — 63 successive wordings of
`forskrift/2015-05-06-455/§4` between 2015 and today, each from the act that made it, closing with
the consolidated current text.

## 11.5 Order matters

`020_reconstruct.sql` must run **after** the amendments, and only fills what is still empty — it
refuses to overwrite wording we actually hold, and skips any interval overlapping an existing
version. Running it first (as the earlier pipeline did) both wasted work and collided with the real
text.

```bash
psql -f schema/001_init.sql -f schema/010_functions.sql
python3 tools/load.py "$DSN" parsed,parsed-lt data      # corpus
python3 tools/amendments.py lovtidend/ amendments.jsonl # extract
python3 tools/load_amendments.py "$DSN" amendments.jsonl# historical wording
psql -f schema/020_reconstruct.sql                      # then fill what is left
```

## 11.6 What would improve it further

- **Repeal and renumber are extracted but not yet applied** as temporal operations — they are
  recorded (5 190 and 2 472) but do not yet close intervals or drive `succeeds` edges.
- **Historical structure.** Attaching sub-paragraph amendments exactly requires reconstructing the
  ledd structure *at the amendment's date*, which means replaying amendments forward from each
  act's original promulgated text rather than matching against today's structure.
- **Per-part entry into force** is parsed, but acts stating different dates per part in prose
  ("del II trer i kraft 1. januar 2021") still fall back to the act-level date.
