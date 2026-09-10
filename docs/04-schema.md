# 04 — Schema

PostgreSQL 16+. Requires `btree_gist` for the mixed-type exclusion constraints.

> The DDL below is shipped as [`schema/001_init.sql`](../schema/001_init.sql) and has been executed
> against a real PostgreSQL 16 cluster; the bitemporal constraints are covered by
> [`schema/test_bitemporal.sql`](../schema/test_bitemporal.sql). Keep the two in sync — the SQL
> files are generated from the fenced blocks here.

## 4.1 Why PostgreSQL

Genuinely bitemporal stores exist — **XTDB** and **Datomic** give you both axes natively and would
remove a real class of bug. The recommendation is still Postgres, for three reasons: range types
plus GiST exclusion constraints get you correct bitemporality with about thirty lines of DDL; the
operational, backup, and hiring story is incomparably better; and the graph traversals here are
shallow (hjemmel chains, citation neighbourhoods), so a dedicated graph store buys little.

If the citation graph later becomes the primary workload rather than a secondary one, revisit —
but do not start there.

Note that Postgres does **not** implement SQL:2011 `PERIOD FOR SYSTEM_TIME`. We build the
equivalent by hand below, which is a feature: the constraint is visible and testable rather than
implicit.

## 4.2 Transaction-time spine

```sql
create extension if not exists btree_gist;

create table snapshot (
    snapshot_id        bigserial primary key,
    source             text        not null,   -- 'lovdata.gjeldende-lover' | 'lovtidend.avd1' | ...
    fetched_at         timestamptz not null,   -- THE transaction timestamp for every derived fact
    content_sha256     bytea       not null,
    byte_size          bigint      not null,
    http_etag          text,
    http_last_modified timestamptz,
    state              text        not null    -- pending|parsed|diffed|validated|promoted|rejected
        check (state in ('pending','parsed','diffed','validated','promoted','rejected')),
    unique (source, fetched_at)
);
create index on snapshot (content_sha256);
```

Every fact derived from one snapshot shares that snapshot's `fetched_at`. Rolling back a bad
ingest is then a single predicate, not an archaeology exercise.

```sql
create table evidence (
    evidence_id bigserial primary key,
    snapshot_id bigint      not null references snapshot,
    method      text        not null   -- footnote0-grammar|lovtidend|snapshot-diff|stortinget|manual
        check (method in ('changesToParent','basedOn','changesToDocuments','lovdata-snapshot',
                          'reconstruction','lovtidend','snapshot-diff','stortinget',
                          'llm-assist','manual')),
    locator     text,                  -- file path + xpath/absoluteaddress inside the snapshot
    raw_excerpt text,                  -- the literal source string, for audit
    confidence  numeric(3,2) not null check (confidence between 0 and 1),
    reviewed_by text,
    reviewed_at timestamptz
);
```

`raw_excerpt` is what lets you answer "why does the database believe this?" with a quotation
rather than an argument.

## 4.3 Works

```sql
create table work (
    work_id      text primary key,         -- 'LOV-2005-06-17-62', 'FOR-2017-12-19-2286'
    doc_type     text not null
        check (doc_type in ('lov','sentral_forskrift','endringslov','endringsforskrift',
                            'instruks','delegering','stortingsvedtak','ukjent')),
    enacted_on   date,
    official_no  integer,
    eli_uri      text,
    published_on date,
    last_corrected date,                -- 'Siste rettelse'; a rettelse, not an amendment
    -- True for a work we know only because something cites it. Change acts are
    -- consumed into the consolidated text and never appear in the current-law
    -- dump, so most cited works are stubs until Lovtidend is ingested.
    is_stub      boolean not null default false,
    first_seen   bigint not null references snapshot,
    last_seen    bigint not null references snapshot
);

-- Work-level attributes that change over time.
create table work_state (
    work_state_id bigserial primary key,
    work_id       text      not null references work,
    language      text      not null default 'nb',   -- 'nb' | 'nn'; see provision.language
    valid         daterange not null,
    tx            tstzrange not null,
    title         text,
    short_title   text,
    ministry      text,
    in_force      boolean   not null,
    repealed_by   text      references work,
    evidence_id   bigint    not null references evidence,
    exclude using gist (work_id with =, language with =, valid with &&, tx with &&)
);
```

That `exclude` clause is the entire bitemporal correctness guarantee: no two rows for the same work
may overlap in **both** axes simultaneously. Overlapping in one is normal and required.

## 4.4 Provisions

```sql
-- Stable identity. One row per logical provision, ever.
create table provision (
    provision_id bigserial primary key,
    work_id      text not null references work,
    -- A work can have parallel language expressions (Grunnloven is published
    -- in bokmål and nynorsk under ONE work id, with identical Lovdata keys),
    -- so language is part of provision identity. ELI carries it the same way.
    language     text not null default 'nb',
    logical_key  text not null,     -- 'lov/2015-04-10-17/§20-1/ledd/1/bokstav/a'
    level        text not null
        check (level in ('del','kapittel','paragraf','ledd','punkt','punktum',
                         'bokstav','seksjon','vedlegg')),
    unique (work_id, language, logical_key)
);

-- Alias table so historical citations to a renumbered provision still resolve.
create table provision_alias (
    provision_id bigint    not null references provision,
    logical_key  text      not null,
    valid        daterange not null,
    primary key (provision_id, logical_key)
);

-- Text stored once per distinct content. Most provisions never change, so this
-- deduplicates the corpus down to a small fraction of its naive size.
create table text_blob (
    sha256 bytea primary key,
    xml    text not null,     -- original Lovdata markup, preserved verbatim
    plain  text not null      -- normalised plain text, for search and diffing
);

create table provision_version (
    pv_id           bigserial primary key,
    provision_id    bigint    not null references provision,
    valid           daterange not null,
    tx              tstzrange not null,
    parent_id       bigint    references provision,
    ordinal         integer   not null,
    designator      text,              -- '§ 5-3'
    heading         text,
    text_sha256     bytea     references text_blob,   -- null when event_known but not text_known
    element_id      text,              -- per-snapshot anchor ('paragraf-6'); UNSTABLE, informational only
    status          text      not null
        check (status in ('in_force','not_yet_in_force','repealed','renumbered','reserved')),
    text_known      boolean   not null default true,
    event_known     boolean   not null default true,
    evidence_id     bigint    not null references evidence,
    exclude using gist (provision_id with =, valid with &&, tx with &&)
);

create index on provision_version (provision_id) where upper_inf(tx);
create index on provision_version using gist (valid) where upper_inf(tx);
```

The two partial indexes matter: "current belief" is the overwhelmingly hot path and deserves an
index that ignores the closed-transaction history entirely.

## 4.5 Edges

```sql
create table edge (
    edge_id     bigserial primary key,
    kind        text not null
        check (kind in ('cites','hasLegalBasis','amends','repeals','replaces','succeeds',
                        'consolidates','entryIntoForceSetBy','correctedBy','enactedBy',
                        'implementsEEA')),
    src_work    text   not null references work,
    src_prov    bigint references provision,
    dst_work    text   references work,
    dst_prov    bigint references provision,
    dst_raw     text,                    -- unresolved reference text, kept when resolution fails
    ref_style   text check (ref_style in ('dynamic','static')),
    ref_pit     date,                    -- point in time, for static references only
    valid       daterange not null,
    tx          tstzrange not null,
    confidence  numeric(3,2) not null check (confidence between 0 and 1),
    evidence_id bigint not null references evidence
);

create index on edge (src_work, kind) where upper_inf(tx);
create index on edge (dst_work, kind) where upper_inf(tx);
create index on edge using gist (valid) where upper_inf(tx);
```

No exclusion constraint here — the same pair of provisions can legitimately carry several edges of
the same kind over one interval (an act may amend a provision twice in one session).

## 4.6 Change events

The reconciliation workspace. These are *extracted claims*, promoted into `provision_version` rows
only once they survive validation.

```sql
create table change_event (
    event_id        bigserial primary key,
    changing_work   text   references work,      -- the endringslov; null if unattributed
    changed_work    text   not null references work,
    changed_prov    bigint references provision, -- null => whole-document operation
    operation       text   not null
        check (operation in ('insert','amend','repeal','renumber','replace_act','correct')),
    kunngjort_on    date,
    in_force_on     date,                        -- null => not stated in the annotation
    -- Lower bound for events that name a changing work but state no date: the
    -- change cannot predate that work. Measured against 59,050 events that DO
    -- state a date, the median gap is 22 days and p95 is 560, so this is a
    -- bound to query with, never a date to display as fact.
    in_force_earliest date,
    in_force_note   text,                        -- 'kongen_bestemmer' | 'straks' | 'virkningstidspunkt'
    in_force_source text                references work,
    retroactive     boolean not null default false,
    state           text    not null
        check (state in ('pending','applied','conflict','rejected')),
    evidence_id     bigint  not null references evidence
);

create index on change_event (state) where state in ('pending','conflict');
create index on change_event (changed_work, in_force_on);
```

`state = 'pending'` is the queue of entries into force awaiting a royal decree
([02 §2.2](02-temporal-model.md#fra-den-tid-kongen-bestemmer)); `state = 'conflict'` is the human
review queue.

## 4.7 The append-only rule

Enforce it, do not merely document it:

```sql
create rule provision_version_no_delete as
    on delete to provision_version do instead nothing;
```

Amendments proceed in exactly two statements, in one transaction:

```sql
-- 1. close the outgoing assertion (transaction time only — valid time is untouched)
update provision_version
   set tx = tstzrange(lower(tx), :snapshot_fetched_at, '[)')
 where provision_id = :pid and upper_inf(tx);

-- 2. assert the new state
insert into provision_version (provision_id, valid, tx, ...)
values (:pid, daterange(:in_force_on, 'infinity', '[)'),
              tstzrange(:snapshot_fetched_at, 'infinity', '[)'), ...);
```

Closing `tx` is the one permitted mutation. Everything else is an insert. A `provision_version`
row, once its transaction interval is closed, is immutable for the life of the database — which is
what allows the system to reproduce any historical answer byte for byte.

## 4.8 Sizing

Rough arithmetic for the retention decision: ~1 000 acts + ~5 100 central regulations, on the order
of 10⁶ provisions. Text is deduplicated by content hash, and on a typical night well under 0.1 % of
provisions change — so steady-state growth is thousands of rows per night, not millions. The
database is a laptop-scale problem; the raw snapshot archive is discussed in
[05](05-pipeline.md#retention).
