-- Regression test for the two properties the whole design rests on:
--   1. no two rows for one entity may overlap in BOTH time axes;
--      overlapping in EITHER ONE alone is normal and required.
--   2. history is append-only.
-- Run against a scratch database after 001_init.sql and 010_functions.sql:
--   psql -v ON_ERROR_STOP=0 -f schema/test_bitemporal.sql

begin;

insert into snapshot(snapshot_id, source, fetched_at, content_sha256, byte_size, state)
values (1, 'lovdata.gjeldende-lover', '2024-01-15', '\x00', 1, 'promoted'),
       (2, 'lovdata.gjeldende-lover', '2026-03-05', '\x01', 1, 'promoted');
insert into evidence(evidence_id, snapshot_id, method, confidence) values (1, 1, 'snapshot-diff', 1.0);
insert into work(work_id, doc_type, first_seen, last_seen) values ('LOV-2005-06-17-62', 'lov', 1, 2);
insert into provision(provision_id, work_id, logical_key, level)
values (1, 'LOV-2005-06-17-62', 'kap:5/§:5-3', 'paragraf');
insert into text_blob(sha256, xml, plain) values ('\xaa', '<p>A</p>', 'A'), ('\xbb', '<p>B</p>', 'B');

\echo '1. initial assertion                                     -> expect INSERT'
insert into provision_version(provision_id, valid, tx, ordinal, status, text_sha256, evidence_id)
values (1, '[2006-01-01,infinity)', '[2024-01-15,2026-03-05)', 1, 'in_force', '\xaa', 1);

\echo '2. same valid period, later belief (a rettelse)          -> expect INSERT'
insert into provision_version(provision_id, valid, tx, ordinal, status, text_sha256, evidence_id)
values (1, '[2006-01-01,infinity)', '[2026-03-05,infinity)', 1, 'in_force', '\xbb', 1);

\echo '3. overlap in BOTH axes                                  -> expect ERROR'
-- savepoint so the expected rejection does not abort the rest of the test
savepoint expect_reject;
insert into provision_version(provision_id, valid, tx, ordinal, status, text_sha256, evidence_id)
values (1, '[2008-01-01,infinity)', '[2026-06-01,infinity)', 1, 'in_force', '\xbb', 1);
rollback to savepoint expect_reject;

\echo '4. earlier valid period, same belief (staged in force)   -> expect INSERT'
insert into provision_version(provision_id, valid, tx, ordinal, status, text_sha256, evidence_id)
values (1, '[2000-01-01,2006-01-01)', '[2026-03-05,infinity)', 1, 'in_force', '\xaa', 1);

\echo '5. history is append-only                                -> expect DELETE 0, 3 rows'
delete from provision_version where provision_id = 1;
select count(*) as surviving_rows from provision_version;

\echo '6. same valid date, two beliefs                          -> expect A then B'
select 'known 2025-01-01' as at, plain from provisions_as_of('LOV-2005-06-17-62','2019-03-03','2025-01-01')
union all
select 'known now()',              plain from provisions_as_of('LOV-2005-06-17-62','2019-03-03', now());

rollback;
