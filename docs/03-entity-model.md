# 03 — Entity model

## 3.1 FRBR layering

Borrowed from Akoma Ntoso, because the alternative — one row per document that gets updated — is
exactly what makes point-in-time queries impossible.

```
Work          LOV-2005-06-17-62                  the act as an abstract thing; identity is eternal
  └ Expression  LOV-2005-06-17-62@2006-01-01     the consolidated text over one valid-time interval
      └ Manifestation  sha256:ab12…/lover/…xml   the concrete file from one snapshot
```

- A **Work** is created once and never versioned. Repeal ends its expressions; it does not delete
  the work. This is what lets a 1902 penal code stay addressable forever.
- An **Expression** is a valid-time slice. In practice we do not materialise whole-document
  expressions as rows — we compose them from provision versions on demand (see
  [07](07-query-surface.md)), because whole-document rows would duplicate an entire act for a
  one-comma amendment.
- A **Manifestation** is the raw ingested file, content-addressed. Keep every distinct one
  forever; see the storage arithmetic in [05](05-pipeline.md#retention).

## 3.2 Identity — the trap worth ten pages

Lovdata's `data-absoluteaddress` is an **ordinal** path: `/chapter/12` for a chapter *displayed* as
"Chapter 10", because chapters 8A and 2-1 consumed ordinals. This has a fatal consequence:

> Inserting `§ 3a` shifts the absolute address of every later paragraph in the act.

If `absoluteaddress` is your primary key, a single insertion looks like a hundred amendments, your
diff explodes, and every stored cross-reference silently retargets to the wrong provision. This
would be the defining bug of the project.

**Therefore: two keys per provision.**

| Key | Example | Stability | Role |
| --- | --- | --- | --- |
| `logical_key` | `kap:5/§:5-3/ledd:2` | Stable across amendments | **Identity.** Foreign keys point here |
| `absoluteaddress` | `/chapter/5/paragraph/17/section/2/` | Per-snapshot | Locator only; a versioned attribute |

`logical_key` is derived from the *printed designators* (§ number, ledd number, letter), normalised:
lowercase, non-breaking spaces stripped, `§ 5-3` → `5-3`, roman numerals folded to arabic with the
original retained. It survives renumbering of *neighbours*, which is the common case.

**Renumbering of the provision itself** — a genuine wholesale renumbering of an act — is rarer and
is handled explicitly: the diff proposes `renumber` edges by content similarity, the old
`logical_key`'s version chain is closed with `status = 'renumbered'`, a `succeeds` edge links old
to new, and an alias row keeps the old key resolvable so that historical citations still work.
Low-confidence renumber proposals go to human review; they are never applied silently.

## 3.3 The node types

| Node | Grain | Notes |
| --- | --- | --- |
| `work` | One act or regulation | `LOV-…`, `FOR-…`; also change acts, which are works in their own right |
| `provision` | Structural fragment | del, kapittel, paragraf, ledd, punktum, bokstav, vedlegg |
| `change_event` | One operation on one provision | Extracted, not ingested; carries confidence |
| `snapshot` | One acquisition run | The transaction-time spine |
| `evidence` | One justification | Every versioned row points at one |

Provisions are versioned bitemporally *individually*. An amendment to one ledd creates one new
`provision_version` row, not a copy of the act. For a corpus of ~5 100 regulations plus ~1 000 acts,
each with hundreds of fragments, this is the difference between a database that fits comfortably
on a laptop and one that does not.

## 3.4 Relationships

All edges are themselves bitemporal — a *hjemmel* is valid only while both endpoints are in force,
and an edge whose target is repealed must be closed, not deleted.

### Structural

- `parentOf` / ordered `hasChild` — the containment tree. Sibling order via `ordinal`.
- Note the tree itself is versioned: a chapter can be moved, split, or merged.

### Referential

- `cites` — an internal or cross-act reference extracted from the text ("jf. § 4", "etter lov om
  behandlingsmåten i forvaltningssaker § 2").
- ❗ **`ref_style`: `dynamic` vs `static`.** Norwegian legal drafting overwhelmingly uses *dynamic*
  references: "jf. forvaltningsloven § 2" means § 2 *as in force when the referring rule is
  applied*, not as it stood when the reference was written. A minority are static ("§ 2 slik den
  lød 1. januar 2010"). Resolving every reference against the citing document's own date is a
  natural-looking mistake that produces subtly wrong answers.
  - `dynamic` → resolve the target at the **query's** valid time.
  - `static` → resolve at the stored `ref_pit`.
- `dst_raw` retains the unresolved reference text, so an unresolvable citation is recorded as
  unresolved rather than dropped.

### Authority

- `hasLegalBasis` (*hjemmel*) — regulation → the statutory provision empowering it. Every central
  regulation declares one; it is in the document metadata.
- This edge earns its keep: when a statutory provision is repealed, every regulation resting on it
  becomes legally questionable. A query for "regulations whose hjemmel is no longer in force at
  time V" is a genuinely novel and useful output of this database, and it falls out of the model
  for free.

### Temporal / derivational

| Edge | Meaning |
| --- | --- |
| `amends` / `isAmendedBy` | Change act → target provision |
| `repeals` / `isRepealedBy` | Ends the target's valid time |
| `replaces` / `isReplacedBy` | New act supersedes an old one wholesale (the user's `isReplaced`) |
| `succeeds` | Version chain within one provision's identity, including renumbering |
| `consolidates` | Expression ← the set of change events baked into it |
| `entryIntoForceSetBy` | Provision → the *kgl. res.* that set its `i kraft` date |
| `correctedBy` | Points at a *rettelse*; the marker distinguishing errata from amendments |

### Enrichment (Phase 5)

- `enactedBy` → Stortinget `sak`/`vedtak` id.
- `implementsEEA` → EU/EEA instrument, from the EØS references common in Norwegian regulations.

## 3.5 Confidence is a first-class column

Ingested facts (the text, the structure, the metadata) are certain. **Derived** facts — extracted
change events, resolved references, proposed renumberings — are not, and the difference must be
visible in the data rather than buried in a log.

Every derived row carries `confidence` (0–1) and an `evidence_id`. The query API can filter on it;
the review UI sorts by it; and a consumer can always ask "show me only what came verbatim from
Lovdata". A legal database that cannot distinguish what it read from what it inferred is not
usable for anything serious.
