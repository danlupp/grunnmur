# 01 — Sources

The design is shaped less by what these APIs offer than by what they withhold. Read the
"does not give us" column first.

## 1.1 Lovdata open data (primary)

Free, unauthenticated, [NLOD 2.0](https://data.norge.no/nlod/no/2.0), rebuilt nightly.

| Dataset | URL | Size |
| --- | --- | --- |
| Current acts | `https://api.lovdata.no/v1/publicData/get/gjeldende-lover.tar.bz2` | small |
| Current central regulations | `https://api.lovdata.no/v1/publicData/get/gjeldende-sentrale-forskrifter.tar.bz2` | ~27 MB compressed, ~5 100 documents |

Format is XML-compatible HTML — openable in a browser, parseable with a standard XML parser.
Documented at `https://api.lovdata.no/xmldocs`; Swagger at `https://api.lovdata.no/swagger`.

**Gives us**

- Consolidated current text of every act and central regulation.
- Structure: `article class=legalArticle` (paragraf), `article class=legalP` (ledd),
  `article class=footnote`, plus chapters and parts.
- ❗ `data-lovdata-URL` on each element, e.g. `SF/forskrift/2000-07-06-727/§5a` — a **stable,
  designator-based key**. (`data-absoluteaddress`, which Lovdata's published documentation
  describes, does **not** occur in this dataset. See [docs/10 §10.1](10-phase0-findings.md).)
- ❗ **`article.changesToParent`** beneath each provision: its change history — 42 161 of them.
  Not "footnote 0", and semi-structured: the changing act is already an `<a href>`. This is the
  richest free source of valid-time evidence in existence for Norwegian law.
- Document metadata: title, short title, date, number, ministry, and — for regulations —
  the *hjemmel* (legal basis).

**Does not give us**

- ❗ Any historical version. Only what is in force today.
- ❗ Repealed acts and regulations. When something is repealed it simply *stops appearing* in the
  dump. Absence is therefore evidence, not a fetch failure — see [05](05-pipeline.md#stage-4).
- Local and municipal regulations (only *sentrale* forskrifter).
- Court decisions and preparatory works (*forarbeider*).
- Any change feed or "what changed last night" endpoint. We must compute it.

> **Phase 0 has run.** The element inventory above is now measured against the real slice in
> `data/`, not taken from Lovdata's published documentation — which turned out to describe a
> different shape. The architecture held; several field-level claims did not. See
> [docs/10 — Phase 0 findings](10-phase0-findings.md).

## 1.2 Norsk Lovtidend, Avdeling I (change events)

The official gazette. Everything that changes a statute or central regulation is promulgated here
first. Published by Lovdata on behalf of the Ministry of Justice.

- Dataset record: `https://data.norge.no/datasets/c0c6a87c-f597-3735-965f-650be23426a0`
- Two distributions, each a ZIP of XML: **2001 → last year-end** (updated annually) and
  **current year** (updated daily).
- NLOD 2.0.
- Documents are also addressable on the web as `lovdata.no/dokument/LTI/lov/<yyyy-mm-dd-nr>`.

**Gives us** the authoritative *change acts* themselves (endringslover, endringsforskrifter):
promulgation date, entry-into-force text, and the operative instructions ("I lov 17. juni 2005
nr. 62 gjøres følgende endringer: § 5-3 andre ledd skal lyde: ...").

> ❗ **Not yet obtained.** `api.lovdata.no`, `lovdata.no` and `data.norge.no` are all refused by
> this environment's egress policy. Lovtidend must be added manually. Its absence is quantified in
> [docs/10 §10.5](10-phase0-findings.md#105--the-gap-norsk-lovtidend): only 10.5 % of the works
> named in change events are present in the current-law slice.

**Does not give us** anything before 2001. Pre-2001 valid time depends entirely on footnote-0
annotations, and pre-2001 *text* is largely unrecoverable from free sources — we will know **that**
a provision changed in 1987 without knowing what it said. The model must represent that
distinction explicitly rather than pretending to a completeness it lacks.

## 1.3 Stortinget open data (legislative provenance)

`https://data.stortinget.no/eksport/...` — XML or JSON, no auth, rate-limited to 100 calls/minute
(HTTP 429 beyond).

Relevant endpoints: `saker`, `sak`, `voteringer?sakid=`, `voteringsvedtak?voteringid=`,
`publikasjoner`.

**Gives us** the parliamentary chain behind each act: case, committee recommendation, votes, and
the *vedtak*. Optional for a point-in-time text query, valuable for "why did this change" and for
detecting a coming change before it reaches Lovtidend.

**Does not give us** consolidated text. Treat it as an enrichment source, wired in at Phase 5.

## 1.4 Source-to-axis mapping

This is the crux of the whole design:

| Source | Contributes | To which axis |
| --- | --- | --- |
| Nightly Lovdata tarball | The text itself; the fact that we observed it on date D | **Transaction time** |
| Footnote 0 in that text | `i kraft` dates per provision, back decades | **Valid time** |
| Lovtidend Avdeling I | Promulgation + entry-into-force of each change act, 2001→ | **Valid time** |
| Snapshot N vs. N−1 diff | Proof that the other two are complete | Reconciliation |
| Stortinget | Enactment provenance and lead time | Attributes |

No single source is sufficient. The pipeline's job is to fuse them and to refuse to guess when
they disagree.

## 1.5 Standards worth borrowing

We are not adopting these wholesale, but we should be able to export to them and should steal
their identity model:

- **ELI** (European Legislation Identifier) — its URI template already carries a *point in time*
  and a *version* component:
  `/eli/{jurisdiction}/{agent}/{year}/{month}/{day}/{type}/{natural id}/{level}/{point in time}/{version}/{language}`.
  Emit an `eli_uri` per work and per expression from day one; it costs nothing now and is
  expensive to retrofit.
- **Akoma Ntoso** — the XML interchange standard for legislative documents, and the source of the
  FRBR Work/Expression/Manifestation layering adopted in [03](03-entity-model.md).

Mapping to Akoma Ntoso on export is a Phase 6 nice-to-have. Mapping our *internal identity model*
onto theirs is a Phase 1 requirement.
