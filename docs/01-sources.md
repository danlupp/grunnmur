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
- `data-absoluteaddress` on each element, e.g. `/chapter/1/paragraph/1/section/1/`, giving every
  element an addressable ordinal path.
- **Footnote 0** directly beneath each paragraph: the change history of that paragraph. This is
  the richest free source of valid-time evidence in existence for Norwegian law.
- Document metadata: title, short title, date, number, ministry, and — for regulations —
  the *hjemmel* (legal basis).

**Does not give us**

- ❗ Any historical version. Only what is in force today.
- ❗ Repealed acts and regulations. When something is repealed it simply *stops appearing* in the
  dump. Absence is therefore evidence, not a fetch failure — see [05](05-pipeline.md#stage-4).
- Local and municipal regulations (only *sentrale* forskrifter).
- Court decisions and preparatory works (*forarbeider*).
- Any change feed or "what changed last night" endpoint. We must compute it.

> **Egress note:** `api.lovdata.no` was blocked by this session's network policy, so the exact
> XML element inventory below is drawn from Lovdata's published documentation rather than from a
> downloaded sample. Phase 0 of the [roadmap](08-roadmap.md) is a schema-discovery spike against a
> real tarball; expect small corrections to element and attribute names, not to the architecture.

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
