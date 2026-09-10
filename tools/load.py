#!/usr/bin/env python3
"""Load parsed JSONL streams into the bitemporal store.

Transaction time comes from the snapshot's fetched_at, so every fact loaded in
one run shares one transaction timestamp and the run is a single rollback unit.

Valid time for the CURRENT text of a provision opens at the latest entry into
force among that provision's own change events, falling back to the work's
dateInForce. Repealed provisions close at their stated data-repealeddate.
Where only a lower bound is known the interval is opened at that bound and the
row is marked so queries can tell a bound from a date.
"""
from __future__ import annotations
import json, sys, pathlib, collections, hashlib, datetime
import psycopg

BATCH = 5000


def rows(path: pathlib.Path):
    with path.open(encoding='utf-8') as fh:
        for line in fh:
            yield json.loads(line)


def load(dsn: str, indir: str, rawdir: str) -> None:
    d = pathlib.Path(indir)
    conn = psycopg.connect(dsn, autocommit=False)
    cur = conn.cursor()

    # ---- snapshots: one per raw archive, content-addressed ----------------
    fetched = datetime.datetime.now(datetime.timezone.utc)
    snap_ids = {}
    for archive in sorted(pathlib.Path(rawdir).glob('*.tar.bz2')):
        sha = hashlib.sha256(archive.read_bytes()).digest()
        source = ('lovdata.gjeldende-lover' if 'lover' in archive.name
                  else 'lovdata.gjeldende-sentrale-forskrifter')
        cur.execute(
            "insert into snapshot(source, fetched_at, content_sha256, byte_size, state)"
            " values (%s,%s,%s,%s,'promoted') returning snapshot_id",
            (source, fetched, sha, archive.stat().st_size))
        snap_ids[source] = cur.fetchone()[0]
    snap = min(snap_ids.values())
    print(f'snapshots: {snap_ids}')

    # ---- shared evidence for facts read verbatim from the snapshot -------
    cur.execute("insert into evidence(snapshot_id, method, confidence, raw_excerpt)"
                " values (%s,'lovdata-snapshot',1.0,'consolidated text as published')"
                " returning evidence_id", (snap,))
    ev_snapshot = cur.fetchone()[0]

    # ---- works, plus stubs for anything referenced but absent ------------
    all_works = list(rows(d / 'works.jsonl'))
    works, seen_w = [], set()
    for w in all_works:            # one work row per id; languages differ below
        if w['work_id'] not in seen_w:
            seen_w.add(w['work_id']); works.append(w)
    present = {w['work_id'] for w in works}
    with cur.copy("copy work(work_id, doc_type, enacted_on, published_on, last_corrected,"
                  " is_stub, first_seen, last_seen) from stdin") as cp:
        for w in works:
            cp.write_row((w['work_id'], w['doc_type'], None, w['published_on'],
                          w['last_corrected'], False, snap, snap))
    print(f'works: {len(works):,}')

    referenced = set()
    for e in rows(d / 'events.jsonl'):
        for k in ('changing_work', 'in_force_source'):
            if e.get(k):
                referenced.add(e[k])
    for e in rows(d / 'edges.jsonl'):
        if e.get('dst_work'):
            referenced.add(e['dst_work'])
    stubs = sorted(referenced - present)
    with cur.copy("copy work(work_id, doc_type, is_stub, first_seen, last_seen) from stdin") as cp:
        for wid in stubs:
            kind = 'lov' if wid.startswith('LOV') else 'sentral_forskrift' if wid.startswith('FOR') else 'ukjent'
            cp.write_row((wid, kind, True, snap, snap))
    print(f'stub works (cited but not in this slice): {len(stubs):,}')

    # ---- work_state: title/ministry, valid from entry into force ---------
    with cur.copy("copy work_state(work_id, language, valid, tx, title, short_title,"
                  " ministry, in_force, evidence_id) from stdin") as cp:
        for w in all_works:        # one state row per language expression
            start = w['date_in_force'] or w['published_on'] or '1000-01-01'
            cp.write_row((w['work_id'], w['language'], f'[{start},)',
                          f'[{fetched.isoformat()},)',
                          w['title'], w['short_title'], w['ministry'], True, ev_snapshot))

    # ---- provisions ------------------------------------------------------
    provs = list(rows(d / 'provisions.jsonl'))
    with cur.copy("copy provision(work_id, language, logical_key, level) from stdin") as cp:
        seen = set()
        for p in provs:
            k = (p['work_id'], p['language'], p['logical_key'])
            if k in seen:
                continue
            seen.add(k)
            cp.write_row((p['work_id'], p['language'], p['logical_key'], p['level']))
    cur.execute("select work_id, language, logical_key, provision_id from provision")
    pid, primary = {}, {}
    for w, lg, k, i in cur.fetchall():
        pid[(w, lg, k)] = i
        # 'nb' is the primary expression where parallel languages exist
        if (w, k) not in primary or lg == 'nb':
            primary[(w, k)] = i
    print(f'provisions: {len(pid):,}')

    # ---- text blobs, deduplicated by content hash ------------------------
    with cur.copy("copy text_blob(sha256, xml, plain) from stdin") as cp:
        written = set()
        for p in provs:
            h = bytes.fromhex(p['text_sha256'])
            if h in written:
                continue
            written.add(h)
            cp.write_row((h, p['text'], p['text']))
    print(f'text blobs: {len(written):,} distinct for {len(provs):,} provisions '
          f'({1 - len(written)/len(provs):.1%} deduplicated)')

    # ---- events ----------------------------------------------------------
    events = list(rows(d / 'events.jsonl'))
    ev_ids = []
    with cur.copy("copy evidence(snapshot_id, method, locator, raw_excerpt, confidence) from stdin") as cp:
        for e in events:
            cp.write_row((snap, 'changesToParent', e['source_file'], e['raw'], e['confidence']))
    cur.execute("select evidence_id from evidence where method='changesToParent' order by evidence_id")
    ev_ids = [r[0] for r in cur.fetchall()]
    assert len(ev_ids) == len(events)

    valid_work = {w['work_id'] for w in works}
    with cur.copy("copy change_event(changing_work, changed_work, changed_prov, operation,"
                  " in_force_on, in_force_earliest, in_force_note, in_force_source, state,"
                  " evidence_id) from stdin") as cp:
        for e, eid in zip(events, ev_ids):
            cp.write_row((
                e['changing_work'], e['changed_work'],
                primary.get((e['changed_work'], e['changed_provision'])),
                e['operation'], e['in_force_on'], e.get('in_force_earliest'),
                e['in_force_note'], e.get('in_force_source'),
                e['state'], eid))
    print(f'change events: {len(events):,}')

    # ---- provision_version: current text, valid from its last change -----
    # A repeal ENDS an interval; it never starts the text version we hold. Taking
    # the max over all events would open the final version on its own repeal date
    # and produce an empty range.
    last_change: dict[int, str] = {}
    for e in events:
        if e['operation'] == 'repeal':
            continue
        p = primary.get((e['changed_work'], e['changed_provision']))
        d0 = e['in_force_on'] or e.get('in_force_earliest')
        if p and d0 and d0 > last_change.get(p, ''):
            last_change[p] = d0
    work_start = {w['work_id']: (w['date_in_force'] or w['published_on'] or '1000-01-01')
                  for w in works}

    n = 0
    with cur.copy("copy provision_version(provision_id, valid, tx, parent_id, ordinal,"
                  " designator, heading, text_sha256, element_id, status, text_known,"
                  " event_known, evidence_id) from stdin") as cp:
        for i, p in enumerate(provs):
            this = pid[(p['work_id'], p['language'], p['logical_key'])]
            start = last_change.get(this) or work_start.get(p['work_id'], '1000-01-01')
            end = p['repealed_on'] or ''
            if end and end <= start:
                # Last recorded change is at or after the repeal: fall back to the
                # work's own start so the interval stays non-empty.
                start = min(work_start.get(p['work_id'], '1000-01-01'), end)
            cp.write_row((
                this, f'[{start},{end})', f'[{fetched.isoformat()},)',
                pid.get((p['work_id'], p['language'], p['parent_key'])) if p['parent_key'] else None,
                p['depth'], p['designator'], p['heading'] or None,
                bytes.fromhex(p['text_sha256']), p['element_id'],
                'repealed' if p['repealed_on'] else 'in_force',
                True, True, ev_snapshot))
            n += 1
    print(f'provision versions: {n:,}')

    # ---- edges -----------------------------------------------------------
    edges = list(rows(d / 'edges.jsonl'))
    cur.execute("insert into evidence(snapshot_id, method, confidence, raw_excerpt)"
                " values (%s,'basedOn',1.0,'document header') returning evidence_id", (snap,))
    ev_edge = cur.fetchone()[0]
    with cur.copy("copy edge(kind, src_work, src_prov, dst_work, dst_prov, dst_raw,"
                  " valid, tx, confidence, evidence_id) from stdin") as cp:
        for e in edges:
            cp.write_row((
                e['kind'], e['src_work'], None, e['dst_work'],
                primary.get((e['dst_work'], e.get('dst_provision'))),
                e.get('dst_provision'),
                f"[{work_start.get(e['src_work'], '1000-01-01')},)",
                f'[{fetched.isoformat()},)', e['confidence'], ev_edge))
    print(f'edges: {len(edges):,}')

    conn.commit()
    print('committed')


if __name__ == '__main__':
    load(sys.argv[1], sys.argv[2], sys.argv[3])
