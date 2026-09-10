-- Historical reconstruction: intervals we know EXISTED but whose text we lack.
--
-- The Lovdata dump carries only current text, so a provision amended in 2015
-- has one text version valid from 2015 onward. Querying it at 2010 would return
-- nothing at all -- indistinguishable from "this provision did not exist".
-- Its change annotations, however, prove it existed and name the dates.
--
-- This fills those gaps with rows carrying text_known = false, so a
-- point-in-time query reports "in force, wording unknown" instead of silence.
-- Reconstructing the WORDING needs the change acts themselves, which live in
-- Norsk Lovtidend and are not in this dataset.

insert into evidence(snapshot_id, method, confidence, raw_excerpt)
select min(snapshot_id), 'reconstruction', 0.5,
       'interval inferred from change-event dates; wording not held'
  from snapshot;

with ev as (
    select ce.changed_prov as provision_id,
           coalesce(ce.in_force_on, ce.in_force_earliest) as d
      from change_event ce
     where ce.changed_prov is not null
       and coalesce(ce.in_force_on, ce.in_force_earliest) is not null
), cur as (
    -- The EARLIEST interval whose wording we hold. Amendment-derived versions
    -- (schema/015 via tools/load_amendments.py) already cover later intervals,
    -- so only what precedes the earliest known wording is still a gap.
    select distinct on (pv.provision_id)
           pv.provision_id, pv.parent_id, pv.ordinal, pv.designator, pv.heading,
           pv.element_id, lower(pv.valid) as cur_start, pv.tx
      from provision_version pv
     where upper_inf(pv.tx) and pv.text_known
     order by pv.provision_id, lower(pv.valid)
), pts as (
    select distinct e.provision_id, e.d
      from ev e join cur c on c.provision_id = e.provision_id
     where e.d < c.cur_start
), seq as (
    select p.provision_id, p.d as from_d,
           lead(p.d) over (partition by p.provision_id order by p.d) as next_d,
           c.cur_start, c.parent_id, c.ordinal, c.designator, c.heading,
           c.element_id, c.tx
      from pts p join cur c on c.provision_id = p.provision_id
)
insert into provision_version(provision_id, valid, tx, parent_id, ordinal, designator,
                              heading, text_sha256, element_id, status,
                              text_known, event_known, evidence_id)
select s.provision_id,
       daterange(s.from_d, coalesce(s.next_d, s.cur_start), '[)'),
       s.tx, s.parent_id, s.ordinal, s.designator, s.heading,
       null, s.element_id, 'in_force',
       false,          -- we do NOT have the wording for this interval
       true,           -- but we do know a change happened here, and when
       (select max(evidence_id) from evidence where method = 'reconstruction')
  from seq s
 where coalesce(s.next_d, s.cur_start) > s.from_d
   -- never collide with wording we actually hold
   and not exists (
       select 1 from provision_version pv
        where pv.provision_id = s.provision_id
          and pv.tx && s.tx
          and pv.valid && daterange(s.from_d, coalesce(s.next_d, s.cur_start), '[)'));
