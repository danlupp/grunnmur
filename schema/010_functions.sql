-- Point-in-time query functions. Design rationale: docs/07-query-surface.md.

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

