# 09 — Risks, limitations, open questions

## 9.1 Hard limits of the source data

These cannot be engineered away. They must be represented honestly in the data model and stated
plainly to users.

| Limit | Consequence | Mitigation |
| --- | --- | --- |
| Dump contains **current law only** | No downloadable history; valid time must be reconstructed | Snapshot from day one; reconstruct backwards from footnotes and Lovtidend |
| Repealed works **disappear** from the dump | Absence must be read as repeal | Absence gate + anomaly detector ([05 stage 4](05-pipeline.md#stage-4)) |
| Lovtidend starts **2001** | Pre-2001 change *events* only via footnotes; pre-2001 *text* largely unrecoverable | `text_known` / `event_known` flags; never interpolate |
| **Local/municipal** regulations excluded | Corpus is incomplete for anything municipal | Document the scope; do not imply completeness |
| No **preparatory works** or case law | No interpretive context | Out of scope; Stortinget gives partial provenance |
| No **change feed** | Change detection is ours to compute | Snapshot diffing is the whole of stages 3–4 |

## 9.2 Technical risks

**Upstream format drift.** Lovdata will change its markup without warning. Mitigation: strict
schema validation in stage 7 with unknown elements as warnings rather than silent drops; keep raw
bytes forever so any night can be re-parsed under a corrected parser. This is the standing argument
for the retention policy.

**Ordinal instability of the positional anchor.** ✅ Confirmed empirically in Phase 0 — and
sidestepped, because Lovdata supplies a stable key we use instead. The residual risk moved: that
stable key is **not unique**, which the exclusion constraint caught during loading. See
[docs/10 §10.1](10-phase0-findings.md).

**Renumbering cascades.** A wholesale renumbering of an act makes the diff explode and defeats
`logical_key` matching. Mitigation: similarity-based move detection, alias rows, and human review
of low-confidence proposals. Accept that these events are rare and cost a manual afternoon.

**Canonicalisation drift.** Changing the normalisation algorithm invalidates every stored content
hash. Mitigation: `canon_version` on every hash, and treat a bump as a full recompute — planned,
not incidental.

**Grammar rot.** Footnote forms will appear that the PEG does not cover. Mitigation: residue queue,
parse-rate alerting, and the discipline of extending the grammar rather than reaching for the LLM
fallback each time.

**Silent LLM plausibility.** An LLM asked to extract a date will produce one whether or not the
text contains it. Mitigation: never primary, always confidence-capped, always routed through review
before affecting a published interval. Reproducibility is the property the whole design is built
to protect.

**Clock and time-zone handling.** Entry into force is Norwegian civil time. Store transaction time
as UTC `timestamptz`; keep valid time as plain dates and never let a timezone conversion shift an
entry-into-force date across midnight — a classic and embarrassing off-by-one.

## 9.3 Legal and ethical caveats

❗ **This is not an authoritative source.** Only Norsk Lovtidend and Lovdata's own publication are
authoritative. A reconstruction — particularly of pre-snapshot valid time — is a best-effort
inference from documented evidence. Every API response and rendered document must carry that
disclaimer, and the `confidence` and `evidence` fields must be exposed rather than hidden behind a
clean façade.

**Attribution.** NLOD 2.0 requires attribution to Lovdata. Include it in API responses, exports,
and any UI.

**Fitness for purpose.** People make decisions with legal consequences from this kind of data. The
`text_known = false` case must be visibly distinguishable from "provision did not exist"; a
low-confidence extracted date must not render identically to one read verbatim from Lovtidend. The
temptation to smooth over gaps for a nicer-looking interface is the most dangerous failure mode in
the whole project, and it is a product decision, not a technical one.

**Rate limits and courtesy.** Stortinget enforces 100 calls/minute. Lovdata publishes nightly —
fetch once per night, send conditional requests, and identify the client honestly in the
User-Agent.

## 9.4 Open questions for Phase 0

1. Is the *hjemmel* machine-readable in the XML, or prose requiring extraction? This determines
   whether `hasLegalBasis` is free or a parsing project.
2. Does the dump include anything about **future-dated** provisions already consolidated but not
   yet in force? If so, `not_yet_in_force` is directly observable rather than inferred — a
   meaningful simplification.
3. Are footnote-0 annotations present on **all** provision levels, or only paragraphs? This sets
   the granularity of reconstructable valid time.
4. How are *vedlegg* (annexes) and tables structured, and do they carry change annotations?
5. Do repealed-but-still-listed provisions appear as empty shells (`(Opphevet ved lov …)`), which
   would give repeal evidence without relying on absence? This would be a significant robustness
   win over the absence gate.
6. What exactly does the Lovtidend XML look like, and does it mark operations structurally or only
   in prose? This is the difference between a day and a fortnight on the Phase 3 grammar.
7. Is there a stable identifier linking a Lovtidend change act to the consolidated work it amends,
   or must it be derived from the date-and-number citation?

**Answered.** Six of the seven were resolved by measurement against the real slice — see
[docs/10 §10.2](10-phase0-findings.md#102-the-seven-open-questions-answered). Five came back more
favourable than assumed (hjemmel, future-dating and repeal are all directly observable).

**Still blocked:** question 6. `api.lovdata.no`, `lovdata.no` and `data.norge.no` are refused by
this environment's egress policy (403 on CONNECT), so Norsk Lovtidend has not been inspected or
ingested. It must be added manually. What it would unlock is quantified in
[docs/10 §10.5](10-phase0-findings.md#105--the-gap-norsk-lovtidend).
