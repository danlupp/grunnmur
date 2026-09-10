#!/usr/bin/env python3
"""Load extracted amendments as historical provision versions.

Amendments supply the one thing the consolidated dump cannot: the WORDING a
provision had before its latest change. Each amendment's text is valid from its
entry into force until the next change to the same provision, at which point the
following amendment -- or, finally, the consolidated current text -- takes over.

Two resolution levels are recorded, because they differ in trustworthiness:

  exact      the amendment's full target key matches a provision
  paragraph  only the § matched; the ledd/punkt key did not

The second is not sloppiness. Sub-paragraph keys are ORDINAL, so "§ 5 andre
ledd" names the ledd structure as it stood on the amendment's date, which need
not be today's. Attaching at § level with `sub_target` recorded says exactly
what is known, rather than asserting a match that may be wrong.

Usage: python3 tools/load_amendments.py <dsn> <amendments.jsonl>
"""
from __future__ import annotations
import json, sys
import psycopg

COLS = ['act', 'part', 'target_work', 'target_key', 'sub_target', 'operation',
        'renamed_to', 'target_kind', 'in_force_on', 'new_text', 'new_text_sha256',
        'instruction', 'source_file', 'confidence']

STAGE = """
drop table if exists amd_stage;
create table amd_stage(
  act text, part text, target_work text, target_key text, sub_target text,
  operation text, renamed_to text, target_kind text, in_force_on date,
  new_text text, new_text_sha256 text, instruction text, source_file text,
  confidence numeric);
"""

ASSEMBLE = """
create index on amd_stage(target_work, target_key);

-- 1. resolve each amendment to a provision, exactly or at paragraph level
drop table if exists amd_resolved;
create table amd_resolved as
select a.*,
       coalesce(px.provision_id, pp.provision_id) as provision_id,
       case when px.provision_id is not null then 'exact' else 'paragraph' end as attach_level
  from amd_stage a
  left join provision px
         on px.work_id = a.target_work and px.logical_key = a.target_key
  left join provision pp
         on pp.work_id = a.target_work
        and pp.logical_key = split_part(a.target_key, '/ledd/', 1)
 where a.target_kind = 'provision'
   and a.in_force_on is not null
   and a.operation in ('amend', 'insert')
   and a.new_text is not null and a.new_text <> '';

delete from amd_resolved where provision_id is null;
create index on amd_resolved(provision_id, in_force_on);

-- 2. one amendment per (provision, date): prefer an exact attachment, then the
--    fullest wording, so a paragraph-level collapse keeps the most complete text
drop table if exists amd_best;
create table amd_best as
select distinct on (provision_id, in_force_on)
       provision_id, in_force_on, new_text, new_text_sha256, attach_level,
       sub_target, act, instruction, confidence
  from amd_resolved
 order by provision_id, in_force_on,
          (attach_level = 'exact') desc, length(new_text) desc;

-- 3. RECONCILE: the consolidated text cannot have begun before the latest
--    amendment we hold for that provision. cur_start was derived from
--    changesToParent, which is only a LOWER BOUND for the 34.8% of events that
--    state no date, so amendments are independent evidence that tightens it.
--    This is assembly-time reconciliation (docs/05 stage 6) on rows not yet
--    published -- not a mutation of an asserted fact.
update provision_version pv
   set valid = daterange(m.max_d, upper(pv.valid), '[)')
  from (select provision_id, max(in_force_on) as max_d from amd_best group by 1) m
 where pv.provision_id = m.provision_id
   and upper_inf(pv.tx) and pv.text_known
   and pv.evidence_id in (select evidence_id from evidence where method = 'lovdata-snapshot')
   and m.max_d > lower(pv.valid)
   and (upper(pv.valid) is null or m.max_d < upper(pv.valid));

-- 4. an amendment's text holds until the next change, or until the consolidated
--    current text takes over. Anything at/after that point is already covered.
drop table if exists amd_iv;
create table amd_iv as
select b.*,
       lead(b.in_force_on) over (partition by b.provision_id order by b.in_force_on) as next_d,
       c.cur_start, c.parent_id, c.ordinal, c.designator, c.heading, c.element_id, c.tx
  from amd_best b
  join (select pv.provision_id, lower(pv.valid) as cur_start, pv.parent_id, pv.ordinal,
               pv.designator, pv.heading, pv.element_id, pv.tx
          from provision_version pv
         where upper_inf(pv.tx) and pv.text_known) c
    on c.provision_id = b.provision_id
 where b.in_force_on < c.cur_start;

-- 5. wording goes into the shared, content-addressed blob store
insert into text_blob(sha256, xml, plain)
select distinct decode(new_text_sha256, 'hex'), new_text, new_text
  from amd_iv
 where not exists (select 1 from text_blob t where t.sha256 = decode(new_text_sha256, 'hex'));

insert into evidence(snapshot_id, method, confidence, raw_excerpt)
select min(snapshot_id), 'lovtidend', 0.9,
       'operative amendment text from a Norsk Lovtidend change act'
  from snapshot;

-- 6. historical versions, clipped so they never reach the current version
insert into provision_version(provision_id, valid, tx, parent_id, ordinal, designator,
                              heading, text_sha256, element_id, status,
                              text_known, event_known, evidence_id)
select provision_id,
       daterange(in_force_on, least(coalesce(next_d, cur_start), cur_start), '[)'),
       tx, parent_id, ordinal, designator, heading,
       decode(new_text_sha256, 'hex'), element_id, 'in_force',
       true, true,
       (select max(evidence_id) from evidence where method = 'lovtidend')
  from amd_iv
 where least(coalesce(next_d, cur_start), cur_start) > in_force_on;
"""


def main(dsn: str, path: str) -> None:
    conn = psycopg.connect(dsn)
    cur = conn.cursor()
    cur.execute(STAGE)
    n = 0
    with cur.copy("copy amd_stage(" + ",".join(COLS) + ") from stdin") as cp:
        for line in open(path, encoding='utf-8'):
            d = json.loads(line)
            cp.write_row(tuple(d.get(c) for c in COLS))
            n += 1
    print(f'staged {n:,} amendments')
    cur.execute(ASSEMBLE)
    for q, label in [
        ("select count(*) from amd_resolved", 'resolved to a provision'),
        ("select count(*) filter (where attach_level='exact') from amd_resolved", '  exact key'),
        ("select count(*) filter (where attach_level='paragraph') from amd_resolved", '  paragraph level'),
        ("select count(*) from amd_iv", 'historical intervals built'),
        ("select count(*) from amd_best b join provision_version pv on pv.provision_id=b.provision_id and upper_inf(pv.tx) and pv.text_known and pv.evidence_id in (select evidence_id from evidence where method='lovdata-snapshot') where b.in_force_on > lower(pv.valid)", '  still after current text'),
        ("select count(*) from provision_version where text_known and evidence_id in"
         " (select evidence_id from evidence where method='lovtidend')", 'versions inserted'),
    ]:
        cur.execute(q)
        print(f'{label:28} {cur.fetchone()[0]:,}')
    conn.commit()


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
