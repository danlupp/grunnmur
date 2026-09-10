# 12 — Citation graph

The reference network between provisions: designed in [docs/03 §3.4](03-entity-model.md#referential)
and [docs/06 §6.4](06-change-extraction.md#64-reference-extraction), built by
[`tools/references.py`](../tools/references.py) and
[`tools/load_references.py`](../tools/load_references.py).

## 12.1 Near-verbatim, not parsed

Lovdata already marks cross-references as links carrying a **provision-level** target:

```html
<a href="lov/2016-06-17-73/§5a">§§ 5a</a>
<a href="lov/1992-11-27-109/eøsl/a123">EØS-avtalen artikkel 123</a>
```

So this is extraction, not prose parsing — which is why it carries far higher confidence than the
amendment grammar, and why 172 754 references come out of the corpus in **27 seconds**.

Two classes of link are deliberately excluded, because they are already modelled and would otherwise
inflate the graph with change provenance dressed as citation:

- links inside `article.changesToParent` — change events and `amends` edges;
- links in the document header (`basedOn`, `changesToDocuments`) — `hasLegalBasis` and `amends`.

The filter is not cosmetic: one act had 121 body links of which only 65 were citations.

## 12.2 ❗ Dynamic by default

Norwegian legal drafting is overwhelmingly **dynamic**: `jf. forvaltningsloven § 2` means § 2 *as in
force when the citing rule is applied*, not as it stood when the citation was written. Static
references are the rare, explicitly-marked case (`slik den lød`, `som gjaldt da`).

Defaulting to static — resolving each reference against the citing document's own date — is the
natural-looking mistake, and it silently returns the wrong text. The measurement supports the
default emphatically: **287 of 172 754 references (0.17 %)** carry a static cue.

- `dynamic` → resolve the target at the **query's** valid time.
- `static` → resolve at the stored `ref_pit`.

## 12.3 Results

| | |
| --- | --- |
| References extracted | **172 754** |
| — `cites` (Norwegian law) | 128 859 |
| — `implementsEEA` (EU/EEA instruments) | 43 895 |
| Cross-document | 111 994 |
| Internal to the citing work | 60 760 |
| Provision-level targets | 107 340 |
| Work-level targets | 65 414 |

Resolution: **96.7 %** of citing provisions resolve (166 989 of 172 754), and **84.1 %** of
provision-level targets (90 246 of 107 340). Unresolved targets keep `dst_raw`, so a citation to a
repealed act is recorded as unresolved rather than dropped.

The structural graph is now:

| Edge | Count | Source |
| --- | --- | --- |
| `hasLegalBasis` | 159 755 | `basedOn`, verbatim |
| `cites` | 128 859 | body links, verbatim |
| `amends` | 52 262 | `changesToDocuments`, verbatim |
| `implementsEEA` | 43 895 | body links, verbatim |
| `succeeds` | 852 | renumber parsing, derived |

Four of the five are verbatim from the source markup.

## 12.4 What it makes possible

**Impact analysis** — what depends on a provision, and therefore what an amendment to it touches:

```sql
select p.logical_key, ws.short_title, count(*) as incoming
  from edge e
  join provision p on p.provision_id = e.dst_prov
  left join work_state ws on ws.work_id = p.work_id and upper_inf(ws.tx)
 where e.kind = 'cites' and upper_inf(e.tx)
 group by 1, 2 order by 3 desc;
```

The top of that list is `forskrift/2013-02-14-199/§5` (narkotikaforskriften's drug schedule) with
**476 incoming citations**, then EØS-loven and several havressurslova provisions — which is what a
Norwegian lawyer would expect, and a useful sanity check that the graph is not noise.

Combined with the two time axes, a citation can be resolved *at a date*: a dynamic reference to a
provision that did not exist in 2010 but exists now resolves differently depending on when you ask.

## 12.5 Limits

- **The graph is current-law only.** References are extracted from the consolidated corpus, so this
  is the citation network as it stands today. Extracting from Lovtidend change acts would give a
  *historical* citation graph, but the citations in a change act belong to the act, not to the law
  it amends, so attributing them correctly needs the same structural work as the amendment texts.
- **Edge valid time is inherited** from the citing provision's current interval: a citation exists
  as long as the wording containing it. That is right in principle, but since most citing provisions
  have one open-ended current interval, most edges are effectively open-ended too.
- **Unlinked textual references are not extracted.** Everything here comes from an `<a>`; a bare
  `jf. § 4` in running text with no link is missed. Measuring that gap is the obvious next check.
