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
        check (method in ('footnote0-grammar','lovtidend','snapshot-diff','stortinget','llm-assist','manual')),
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
                            'instruks','delegering','stortingsvedtak')),
    enacted_on   date,
    official_no  integer,
    eli_uri      text,
    first_seen   bigint not null references snapshot,
    last_seen    bigint not null references snapshot
);

-- Work-level attributes that change over time.
create table work_state (
    work_state_id bigserial primary key,
    work_id       text      not null references work,
    valid         daterange not null,
    tx            tstzrange not null,
    title         text,
    short_title   text,
    ministry      text,
    in_force      boolean   not null,
    repealed_by   text      references work,
    evidence_id   bigint    not null references evidence,
    exclude using gist (work_id with =, valid with &&, tx with &&)
);

-- Stable identity. One row per logical provision, ever.
create table provision (
    provision_id bigserial primary key,
    work_id      text not null references work,
    logical_key  text not null,     -- 'kap:5/§:5-3/ledd:2'  — see docs/03 §3.2
    level        text not null
        check (level in ('del','kapittel','paragraf','ledd','punktum','bokstav','vedlegg')),
    unique (work_id, logical_key)
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
    absoluteaddress text,              -- per-snapshot locator; informational only
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
    in_force_on     date,                        -- null => 'fra den tid Kongen bestemmer'
    in_force_source text,                        -- 'res. 17 juni 2005 nr. 603'
    retroactive     boolean not null default false,
    state           text    not null
        check (state in ('pending','applied','conflict','rejected')),
    evidence_id     bigint  not null references evidence
);

create index on change_event (state) where state in ('pending','conflict');
create index on change_event (changed_work, in_force_on);

create rule provision_version_no_delete as
    on delete to provision_version do instead nothing;

