#!/usr/bin/env python3
"""Load the extracted citation graph as `cites` / `implementsEEA` edges.

An edge's valid time is taken from the citing provision's own current interval:
the citation exists exactly as long as the wording that contains it. Targets are
resolved to a provision where the link is provision-level and that provision is
held; otherwise the edge keeps `dst_raw` so an unresolvable citation is recorded
as unresolved rather than dropped.

Usage: python3 tools/load_references.py <dsn> <refs.jsonl>
"""
from __future__ import annotations
import json, sys
import psycopg

COLS = ['kind', 'src_work', 'language', 'src_provision', 'dst_work', 'dst_provision',
        'dst_raw', 'internal', 'ref_style', 'link_text', 'confidence', 'source_file']

STAGE = """
drop table if exists ref_stage;
create table ref_stage(
  kind text, src_work text, language text, src_provision text, dst_work text,
  dst_provision text, dst_raw text, internal boolean, ref_style text,
  link_text text, confidence numeric, source_file text);
"""

LOAD = """
create index on ref_stage(src_work, src_provision);
create index on ref_stage(dst_work, dst_provision);

-- Idempotent: a re-run replaces the citation graph rather than doubling it.
delete from edge where kind in ('cites', 'implementsEEA');

insert into evidence(snapshot_id, method, confidence, raw_excerpt)
select min(snapshot_id), 'lovdata-snapshot', 1.0,
       'cross-reference link in the consolidated text' from snapshot;

drop table if exists ref_resolved;
create table ref_resolved as
select r.*,
       ps.provision_id as src_prov_id,
       pd.provision_id as dst_prov_id,
       sv.valid as src_valid, sv.tx as src_tx
  from ref_stage r
  left join provision ps
         on ps.work_id = r.src_work and ps.logical_key = r.src_provision
        and ps.language = r.language
  left join provision pd
         on pd.work_id = r.dst_work and pd.logical_key = r.dst_provision
        and pd.language = 'nb'
  left join lateral (
        select pv.valid, pv.tx from provision_version pv
         where pv.provision_id = ps.provision_id and upper_inf(pv.tx)
         order by lower(pv.valid) desc limit 1) sv on true
 where exists (select 1 from work w where w.work_id = r.src_work);

insert into edge(kind, src_work, src_prov, dst_work, dst_prov, dst_raw,
                 ref_style, valid, tx, confidence, evidence_id)
select r.kind, r.src_work, r.src_prov_id,
       case when exists (select 1 from work w where w.work_id = r.dst_work)
            then r.dst_work else null end,
       r.dst_prov_id, r.dst_raw, r.ref_style,
       coalesce(r.src_valid, daterange(null, null, '[)')),
       coalesce(r.src_tx, tstzrange((select max(fetched_at) from snapshot), null, '[)')),
       case when r.dst_prov_id is not null then 1.0
            when r.dst_work is not null then 0.8 else 0.5 end,
       (select max(evidence_id) from evidence
         where method = 'lovdata-snapshot' and raw_excerpt like 'cross-reference%')
  from ref_resolved r;
"""


def main(dsn: str, path: str) -> None:
    conn = psycopg.connect(dsn)
    cur = conn.cursor()
    cur.execute(STAGE)
    n = 0
    with cur.copy("copy ref_stage(" + ",".join(COLS) + ") from stdin") as cp:
        for line in open(path, encoding='utf-8'):
            d = json.loads(line)
            cp.write_row(tuple(d.get(c) for c in COLS))
            n += 1
    print(f'staged {n:,} references')
    cur.execute(LOAD)
    for q, label in [
        ("select count(*) from ref_resolved", 'loadable (citing work held)'),
        ("select count(*) filter (where src_prov_id is not null) from ref_resolved", '  citing provision resolved'),
        ("select count(*) filter (where dst_prov_id is not null) from ref_resolved", '  target provision resolved'),
        ("select count(*) filter (where dst_prov_id is null and dst_work is not null) from ref_resolved", '  target work only'),
        ("select count(*) from edge where kind='cites'", "cites edges"),
        ("select count(*) from edge where kind='implementsEEA'", 'implementsEEA edges'),
    ]:
        cur.execute(q)
        print(f'{label:32} {cur.fetchone()[0]:,}')
    conn.commit()


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
