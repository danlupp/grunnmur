-- grunnmur — bitemporal schema for Norwegian law.
-- Design rationale: docs/04-schema.md. Verified against PostgreSQL 16.
-- Every fact is append-only; closing a transaction interval is the only permitted mutation.

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

create rule provision_version_no_delete as
    on delete to provision_version do instead nothing;

