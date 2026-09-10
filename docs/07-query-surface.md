# 07 — Query surface

Everything is a function of `(valid_at, known_at)`. Default `known_at = now()`, so casual users get
current belief and never think about the second axis; auditors pass it explicitly.

## 7.1 Point-in-time provisions

```sql
create or replace function provisions_as_of(
    p_work    text,
    p_valid   date,
    p_known   timestamptz default now()
) returns table (
    provision_id bigint,
    logical_key  text,
    designator   text,
    heading      text,
    plain        text,
    ordinal      int,
    parent_id    bigint,
    text_known   boolean
)
language sql stable as $$
    select pv.provision_id, p.logical_key, pv.designator, pv.heading,
           tb.plain, pv.ordinal, pv.parent_id, pv.text_known
      from provision_version pv
      join provision  p  using (provision_id)
      left join text_blob tb on tb.sha256 = pv.text_sha256
     where p.work_id = p_work
       and pv.valid @> p_valid
       and pv.tx    @> p_known
       and pv.status = 'in_force'
     order by pv.ordinal;
$$;
```

Both predicates are range containment, so both are index-supported. The `left join` on `text_blob`
is deliberate: a provision with `text_known = false` returns a row with null text and an explicit
flag, rather than vanishing. Callers must be able to tell "no such provision" from "we know this
existed but not what it said" — see [02 §2.2](02-temporal-model.md#the-reconstruction-horizon).

## 7.2 The corpus in force at a point in time

The user's headline requirement — the current *lov + forskrift* as of a given date:

```sql
create or replace function corpus_as_of(
    p_valid date,
    p_known timestamptz default now()
) returns table (work_id text, doc_type text, title text)
language sql stable as $$
    select w.work_id, w.doc_type, ws.title
      from work_state ws
      join work w using (work_id)
     where ws.valid @> p_valid
       and ws.tx    @> p_known
       and ws.in_force
     order by w.doc_type, w.work_id;
$$;
```

## 7.3 Document assembly

Walk the provision tree at `(valid, known)` and render. Recursive CTE over the versioned
`parent_id`/`ordinal` — note that the *tree itself* is versioned, so the walk must resolve
parentage at the query's coordinates, not from a current-state tree:

```sql
with recursive tree as (
    select * from provisions_as_of(:work, :valid, :known) where parent_id is null
    union all
    select c.* from provisions_as_of(:work, :valid, :known) c join tree t on c.parent_id = t.provision_id
)
select * from tree;
```

Render to HTML, plain text, or Akoma Ntoso. Cache assembled documents keyed by
`(work_id, valid, known, renderer_version)` — the underlying rows are immutable, so the cache never
needs invalidation, only eviction.

## 7.4 Temporal diff — "what changed between two dates"

```sql
select p.logical_key,
       old.plain as before,
       new.plain as after
  from provision p
  left join lateral (select tb.plain from provision_version pv
                     left join text_blob tb on tb.sha256 = pv.text_sha256
                     where pv.provision_id = p.provision_id
                       and pv.valid @> :d1 and pv.tx @> :known) old on true
  left join lateral (select tb.plain from provision_version pv
                     left join text_blob tb on tb.sha256 = pv.text_sha256
                     where pv.provision_id = p.provision_id
                       and pv.valid @> :d2 and pv.tx @> :known) new on true
 where p.work_id = :work
   and old.plain is distinct from new.plain;
```

Holding `known` fixed while varying `valid` gives you *legal* change. Holding `valid` fixed while
varying `known` gives you *knowledge* change — corrections, late consolidation, and our own
extraction bugs. Two different questions that a single-axis database cannot tell apart.

## 7.5 Graph queries the model makes cheap

**Orphaned regulations** — regulations whose *hjemmel* was no longer in force on a given date.
This is a genuinely useful legal-hygiene report and falls straight out of the schema:

```sql
select e.src_work
  from edge e
  join provision_version pv on pv.provision_id = e.dst_prov
 where e.kind = 'hasLegalBasis'
   and e.valid @> :d and e.tx @> :known
   and not exists (
       select 1 from provision_version pv2
        where pv2.provision_id = e.dst_prov
          and pv2.valid @> :d and pv2.tx @> :known
          and pv2.status = 'in_force');
```

**Impact analysis** — everything citing a provision, at a date: filter `edge` on
`kind = 'cites' and dst_prov = :pid`. Combined with `ref_style = 'dynamic'`, this answers "what
else is affected if we amend this?", which is the question legislative drafters actually have.

**Version chain** — follow `succeeds` edges to trace a provision across renumberings.

## 7.6 API

A thin read-only HTTP layer over the functions above:

```
GET /work/{id}?valid=2019-03-03&known=2020-06-01
GET /work/{id}/provision/{logical_key}?valid=…&known=…
GET /corpus?valid=…&known=…&type=lov
GET /work/{id}/diff?from=2019-01-01&to=2020-01-01
GET /work/{id}/timeline           → change events + intervals
GET /provision/{id}/citations?valid=…
GET /pending                      → promulgated, awaiting entry into force or consolidation
```

Two response conventions that keep the system honest:

- **Every response echoes the resolved `(valid, known)` coordinates and the `snapshot_id` its facts
  came from.** A response that does not say what it is a view *of* is not reproducible.
- **Every derived field carries its `confidence` and `evidence` link.** Consumers who want only
  verbatim Lovdata content can filter on `evidence.method`; those who want the reconstruction get
  it clearly labelled as such.

Content negotiation: JSON by default, Akoma Ntoso XML and the original Lovdata markup available.
Emit `eli_uri` on every work and expression, and — since assembled documents are immutable — serve
them with long `Cache-Control` and an ETag over `(work, valid, known, renderer_version)`.
