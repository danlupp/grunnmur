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
        'derived', 'instruction', 'source_file', 'confidence']

STAGE = """
drop table if exists amd_stage;
create table amd_stage(
  act text, part text, target_work text, target_key text, sub_target text,
  operation text, renamed_to text, target_kind text, in_force_on date,
  new_text text, new_text_sha256 text,
  -- 'paragraph' = the instruction's own target; 'ledd'/'punkt' = sub-structure
  -- read out of a whole-paragraph replacement, which carries its own ledd.
  derived text,
  instruction text, source_file text, confidence numeric);
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

REPEALS_RENUMBERS = """
-- ============================ REPEALS ============================
-- ❗ EXACT attachment only. A repeal of "§ 5 andre ledd" that resolved only at
-- § level must NOT close § 5 -- that would repeal a whole paragraph on the
-- strength of a key we already know did not match. Paragraph-level repeals are
-- recorded as unapplied instead.
drop table if exists rep_resolved;
create table rep_resolved as
select a.act, a.target_work, a.target_key, a.sub_target, a.in_force_on,
       a.instruction, px.provision_id
  from amd_stage a
  join provision px
    on px.work_id = a.target_work and px.logical_key = a.target_key
 where a.operation = 'repeal' and a.in_force_on is not null;

-- Classify against the text we currently hold. A provision still present in the
-- consolidated dump but repealed in the past is a contradiction, not a fact to
-- apply silently.
drop table if exists rep_action;
create table rep_action as
select distinct on (r.provision_id) r.*, pv.pv_id, lower(pv.valid) as cur_start,
       upper(pv.valid) as cur_end, pv.status,
       case
         -- ❗ "§ 42 annet punktum oppheves" removes a SENTENCE, but its key
         -- exact-matches the whole § 42. Applying it would repeal a paragraph
         -- on the strength of an instruction that never said so. Sentences are
         -- finer than this corpus models, so these are recorded, never applied.
         when r.sub_target is not null                     then 'sub_paragraph_target'
         when pv.status = 'repealed'                       then 'already_repealed'
         when r.in_force_on <= lower(pv.valid)             then 'predates_current_text'
         when upper(pv.valid) is not null
              and r.in_force_on >= upper(pv.valid)         then 'already_closed_earlier'
         -- ❗ A provision still standing in the current-law dump cannot have
         -- been repealed in the past. Only a FUTURE repeal is unambiguous;
         -- a past one contradicts the snapshot and is a conflict to review.
         when r.in_force_on <= (select max(fetched_at)::date from snapshot)
                                                           then 'past_repeal_still_present'
         else                                                   'apply'
       end as action
  from rep_resolved r
  join provision_version pv
    on pv.provision_id = r.provision_id and upper_inf(pv.tx)
   and pv.evidence_id in (select evidence_id from evidence where method = 'lovdata-snapshot')
 order by r.provision_id, r.in_force_on;

-- Apply: close the final interval at the repeal date. Assembly-time, on rows
-- not yet published (docs/05 stage 6), same as the cur_start reconciliation.
update provision_version pv
   set valid = daterange(lower(pv.valid), r.in_force_on, '[)'),
       status = 'repealed'
  from rep_action r
 where pv.pv_id = r.pv_id and r.action = 'apply';

-- Contradictions and unapplied repeals are recorded, never dropped.
insert into evidence(snapshot_id, method, confidence, raw_excerpt)
select min(snapshot_id), 'lovtidend', 0.9, 'repeal instruction from a change act' from snapshot;

insert into change_event(changing_work, changed_work, changed_prov, operation,
                         in_force_on, state, evidence_id)
select r.act, r.target_work, r.provision_id, 'repeal', r.in_force_on,
       case when r.action = 'apply' then 'applied' else 'conflict' end,
       (select max(evidence_id) from evidence where method = 'lovtidend')
  from rep_action r
 where exists (select 1 from work w where w.work_id = r.act);

-- ============================ RENUMBERS ============================
-- "§ 3 blir ny § 4" is resolved on the NEW key: after the renumbering the
-- provision lives at § 4, so that is what exists in current law. The OLD key
-- becomes an alias so historical citations to § 3 still resolve.
drop table if exists ren_resolved;
create table ren_resolved as
select distinct a.act, a.target_work, a.target_key as old_key, a.renamed_to as new_key,
       a.in_force_on, pn.provision_id
  from amd_stage a
  join provision pn
    on pn.work_id = a.target_work and pn.logical_key = a.renamed_to
 where a.operation = 'renumber' and a.renamed_to is not null
   and a.renamed_to <> a.target_key;

insert into provision_alias(provision_id, logical_key, valid)
select distinct on (provision_id, old_key)
       provision_id, old_key,
       daterange(null, in_force_on, '[)')      -- the old designator, up to the change
  from ren_resolved
 where not exists (select 1 from provision_alias pa
                    where pa.provision_id = ren_resolved.provision_id
                      and pa.logical_key = ren_resolved.old_key)
 order by provision_id, old_key, in_force_on;

insert into edge(kind, src_work, src_prov, dst_work, dst_prov, dst_raw,
                 valid, tx, confidence, evidence_id)
select distinct 'succeeds', r.target_work, r.provision_id, r.target_work,
       po.provision_id, r.old_key,
       daterange(r.in_force_on, null, '[)'),
       (select tx from provision_version pv where pv.provision_id = r.provision_id
         and upper_inf(pv.tx) limit 1),
       1.0, (select max(evidence_id) from evidence where method = 'lovtidend')
  from ren_resolved r
  left join provision po
    on po.work_id = r.target_work and po.logical_key = r.old_key
 where r.in_force_on is not null;
"""

HISTORICAL_PROVISIONS = """
-- ============ provisions that no longer exist in current law ============
-- A repealed provision vanishes from the consolidated dump, so every amendment
-- and repeal aimed at it has had nowhere to attach. But the change acts DO
-- carry its wording and its dates. These rows reconstruct it: origin =
-- 'lovtidend' marks a provision known only from change acts, never from a
-- current-law snapshot, so it can always be filtered out.
drop table if exists hist_keys;
create table hist_keys as
select distinct a.target_work, a.target_key
  from amd_stage a
 where a.target_kind = 'provision'
   and a.sub_target is null
   -- the WORK is loaded (so this is not merely an unknown act) ...
   and exists (select 1 from provision p where p.work_id = a.target_work)
   -- ... but the provision is not, i.e. it has since been removed
   and not exists (select 1 from provision p
                    where p.work_id = a.target_work and p.logical_key = a.target_key)
   -- and we hold actual wording for it, so the row is evidence, not a guess
   and exists (select 1 from amd_stage b
                where b.target_work = a.target_work and b.target_key = a.target_key
                  and b.operation in ('amend','insert')
                  and b.new_text is not null and b.new_text <> ''
                  and b.in_force_on is not null);

insert into provision(work_id, language, logical_key, level, origin)
select target_work, 'nb', target_key,
       case when target_key ~ '/punkt/[0-9]+$' then 'punkt'
            when target_key ~ '/ledd/[0-9]+$'  then 'ledd'
            else 'paragraf' end,
       'lovtidend'
  from hist_keys;

insert into evidence(snapshot_id, method, confidence, raw_excerpt)
select min(snapshot_id), 'lovtidend', 0.7,
       'provision reconstructed from change acts; absent from current law' from snapshot;

-- one wording per (provision, date), fullest text winning
drop table if exists hist_amd;
create table hist_amd as
select distinct on (p.provision_id, a.in_force_on)
       p.provision_id, a.in_force_on, a.new_text, a.new_text_sha256
  from hist_keys k
  join provision p on p.work_id = k.target_work and p.logical_key = k.target_key
                  and p.origin = 'lovtidend'
  join amd_stage a on a.target_work = k.target_work and a.target_key = k.target_key
 where a.operation in ('amend','insert') and a.in_force_on is not null
   and a.new_text is not null and a.new_text <> ''
 order by p.provision_id, a.in_force_on, length(a.new_text) desc;

-- the earliest repeal we hold for it, if any
drop table if exists hist_rep;
create table hist_rep as
select p.provision_id, min(a.in_force_on) as repeal_on
  from hist_keys k
  join provision p on p.work_id = k.target_work and p.logical_key = k.target_key
                  and p.origin = 'lovtidend'
  join amd_stage a on a.target_work = k.target_work and a.target_key = k.target_key
 where a.operation = 'repeal' and a.in_force_on is not null
 group by p.provision_id;

insert into text_blob(sha256, xml, plain)
select distinct decode(new_text_sha256,'hex'), new_text, new_text from hist_amd h
 where not exists (select 1 from text_blob t where t.sha256 = decode(h.new_text_sha256,'hex'));

-- Each wording holds until the next one; the last until the repeal date. Where
-- no repeal date is known the end is bounded by the snapshot in which the
-- provision is already absent -- an upper bound, not a known date, which is why
-- these rows carry the lower-confidence evidence row above.
insert into provision_version(provision_id, valid, tx, ordinal, designator, text_sha256,
                              status, text_known, event_known, evidence_id)
select h.provision_id,
       daterange(h.in_force_on, e.end_d, '[)'),
       tstzrange((select max(fetched_at) from snapshot), null, '[)'),
       0, null, decode(h.new_text_sha256,'hex'),
       case when e.next_d is null and r.repeal_on is not null then 'repealed' else 'in_force' end,
       true, true,
       (select max(evidence_id) from evidence where method = 'lovtidend')
  from (select h.*, lead(in_force_on) over (partition by provision_id order by in_force_on) as next_d
          from hist_amd h) h
  left join hist_rep r on r.provision_id = h.provision_id
  cross join lateral (select coalesce(h.next_d, r.repeal_on,
                                      (select max(fetched_at)::date from snapshot)) as end_d,
                             h.next_d as next_d) e
 where e.end_d > h.in_force_on;

-- Recovered provisions inherit structure from their own keys: a key is built as
-- <paragraph>/ledd/N[/punkt/M], so the parent is the key with its last level
-- stripped, and the ordinal is that level's number. This is why keys are worth
-- deriving in Lovdata's own idiom -- the tree falls out of them.
update provision_version pv
   set parent_id = par.provision_id,
       ordinal   = coalesce((regexp_match(p.logical_key, '/(?:ledd|punkt)/([0-9]+)$'))[1]::int,
                            pv.ordinal),
       designator = coalesce(pv.designator,
                             (regexp_match(p.logical_key, '(§[0-9A-Za-zæøåÆØÅ-]+)$'))[1])
  from provision p
  left join provision par
         on par.work_id = p.work_id
        and par.logical_key = regexp_replace(p.logical_key, '/(ledd|punkt)/[0-9]+$', '')
        and par.logical_key <> p.logical_key
 where pv.provision_id = p.provision_id
   and p.origin = 'lovtidend'
   and pv.parent_id is null;
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
    cur.execute(REPEALS_RENUMBERS)
    cur.execute(HISTORICAL_PROVISIONS)
    for q, label in [
        ("select count(*) from amd_resolved", 'resolved to a provision'),
        ("select count(*) filter (where attach_level='exact') from amd_resolved", '  exact key'),
        ("select count(*) filter (where attach_level='paragraph') from amd_resolved", '  paragraph level'),
        ("select count(*) from amd_iv", 'historical intervals built'),
        ("select count(*) from amd_best b join provision_version pv on pv.provision_id=b.provision_id and upper_inf(pv.tx) and pv.text_known and pv.evidence_id in (select evidence_id from evidence where method='lovdata-snapshot') where b.in_force_on > lower(pv.valid)", '  still after current text'),
        ("select count(*) from provision_version where text_known and evidence_id in"
         " (select evidence_id from evidence where method='lovtidend')", 'versions inserted'),
        ("select count(*) from rep_resolved", 'repeals resolved exactly'),
        ("select count(*) filter (where action='apply') from rep_action", '  applied'),
        ("select count(*) filter (where action<>'apply') from rep_action", '  recorded, not applied'),
        ("select count(*) from ren_resolved", 'renumbers resolved'),
        ("select count(*) from provision_alias", '  aliases created'),
        ("select count(*) from edge where kind='succeeds'", "  succeeds edges"),
        ("select count(*) from provision where origin='lovtidend'", 'provisions recovered (gone from current law)'),
        ("select count(*) from provision_version pv join provision p using(provision_id)"
         " where p.origin='lovtidend'", '  their wordings'),
        ("select count(*) from provision_version pv join provision p using(provision_id)"
         " where p.origin='lovtidend' and pv.parent_id is not null", '  with a parent'),
    ]:
        cur.execute(q)
        print(f'{label:28} {cur.fetchone()[0]:,}')
    conn.commit()


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
