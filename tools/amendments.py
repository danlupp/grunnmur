#!/usr/bin/env python3
"""Extract operative amendments from Norsk Lovtidend change acts.

A change act states its edits as an instruction paragraph followed by the new
wording:

    I lov 6. juni 1975 nr. 29 om eigedomsskatt til kommunane gjøres følgende endringer:
    § 24 skal lyde:
    § 24. Eigedomsskatten skal svarast til den kommunen der skatten er utskriven.
    § 25 annet ledd skal lyde:
    Kommunen kan i særlege høve gjeva utsetjing.

This turns each of those into (target work, target provision, operation, new
wording, entry into force) -- which is what supplies historical WORDING, as
opposed to the change *dates* that `changesToParent` annotations already give.

Change acts divide into roman-numeral parts that may carry different entry-into-
force dates, so dates are resolved per part, not per act.

Usage: python3 tools/amendments.py <lovtidend-dir> <outfile.jsonl>
"""
from __future__ import annotations
import re, sys, json, pathlib, collections
import lxml.html

from lovdata import canon, sha256, nor_date, work_id_from_ref, NOR_DATE, MONTHS  # noqa: F401

ORDINALS = {
    'første': 1, 'fyrste': 1, 'andre': 2, 'annet': 2, 'anna': 2, 'tredje': 3,
    'fjerde': 4, 'femte': 5, 'sjette': 6, 'sjuende': 7, 'syvende': 7, 'sjuande': 7,
    'åttende': 8, 'åttande': 8, 'niende': 9, 'niande': 9, 'tiende': 10, 'tiande': 10,
    'ellevte': 11, 'tolvte': 12, 'trettende': 13, 'fjortende': 14, 'femtende': 15,
}
ORD_ALT = '|'.join(sorted(ORDINALS, key=len, reverse=True))

# "I lov 6. juni 1975 nr. 29 om ..." / "I forskrift 17. des 2004 nr. 1852 ..."
TARGET_WORK = re.compile(
    rf'\bI\s+(lov|forskrift|vedtak)\s+(\d{{1,2}})\.?\s+({"|".join(MONTHS)})\.?\s+(\d{{4}})'
    rf'\s+nr\.?\s*(\d+)', re.I)

# Operative verbs. Anchoring matters and differs per verb:
#   "skal lyde" ENDS the line -- the new wording follows in the next elements.
#   "oppheves" / "utgår" end the line but may carry trailing punctuation.
#   "blir ny § 4" is mid-line, with the new designator after the verb.
LYDE = re.compile(r'\b(skal lyde|skal lyda|skal ha denne ordlyden)\b\s*[:.]?\s*$', re.I)
REPEAL = re.compile(r'\b(oppheves|opphevast|oppheves med virkning|utgår|utgjeng)\b\s*[.:]?\s*$', re.I)
RENUM = re.compile(r'\b(blir|vert)\s+(ny|nytt|nye)\b', re.I)
# "§ 25 annet ledd første punktum", "§ 2 første ledd bokstav a", "§ 5 nr. 3"
PARAGRAF = re.compile(r'§\s*(\d+[a-zA-ZæøåÆØÅ]*(?:-\d+[a-zA-ZæøåÆØÅ]*)*)')
SUBDIV = re.compile(
    rf'\b(?:(ny|nytt|nye)\s+)?(?:({ORD_ALT})|(\d+))\s*(ledd|punktum|punkt)\b', re.I)
BOKSTAV = re.compile(r'\bbokstav(?:ene)?\s+([a-zæøå])\b', re.I)
NUMMER = re.compile(r'\bnr\.?\s*(\d+)\b', re.I)
NEW_MARK = re.compile(r'\b(ny|nytt|nye)\b', re.I)
# Instructions that target something other than a numbered provision.
NON_PARAGRAF = re.compile(
    r'\b(vedlegg|innledningsteksten|overskriften|EØS-henvisning\w*|'
    r'tittelen|kapittel\w*|del\s+[IVX]+|skjema\w*|tabell\w*)\b', re.I)
IN_FORCE = re.compile(r'\btrer?\s+i\s+kraft\b|\btrer\s+i\s+kraft\b|\bgjelder\s+fra\b|'
                      r'\btek\s+til\s+å\s+gjelde\b', re.I)


def instruction_op(text: str) -> str | None:
    """Operation for an instruction line, or None if it is not one."""
    m = LYDE.search(text)
    if m:
        # "Ny § 5a skal lyde" / "§ 3 nytt tredje ledd skal lyde" insert, not amend.
        return 'insert' if NEW_MARK.search(text[:m.start()]) else 'amend'
    if REPEAL.search(text):
        return 'repeal'
    if RENUM.search(text) and PARAGRAF.search(text):
        return 'renumber'
    return None


def target_key(text: str, work_ref: str) -> tuple[str | None, str | None]:
    """(logical_key, sub_target) for an instruction line.

    sub_target names a granularity finer than the corpus models -- a `punktum`
    is a sentence inside a ledd, not a node -- so the amendment attaches to the
    containing node and records what it actually touched.
    """
    pm = PARAGRAF.search(text)
    if not pm:
        return None, None
    key = f'{work_ref}/§{pm.group(1)}'
    sub = None
    tail = text[pm.end():]
    for m in SUBDIV.finditer(tail):
        n = ORDINALS.get((m.group(2) or '').lower()) or (int(m.group(3)) if m.group(3) else None)
        if not n:
            continue
        level = m.group(4).lower()
        if level == 'punktum':          # a sentence: finer than we model
            sub = f'punktum/{n}'
            break
        key += f'/{"ledd" if level == "ledd" else "punkt"}/{n}'
    bm = BOKSTAV.search(tail)
    if bm:
        key += f'/punkt/{ord(bm.group(1).lower()) - ord("a") + 1}'
    else:
        nm = NUMMER.search(tail)
        if nm and 'nr' in tail.lower():
            key += f'/punkt/{nm.group(1)}'
    return key, sub


def is_instruction(el) -> bool:
    cls = (el.get('class') or '')
    txt = canon(el.text_content())
    return ('defaultP' in cls or 'miscHeadline' in cls) and 5 < len(txt) < 200 \
        and instruction_op(txt) is not None


def part_target_work(part_text: str, fallback: str | None) -> str | None:
    m = TARGET_WORK.search(part_text)
    if m:
        kind, d, mon, y, nr = m.groups()
        mo = MONTHS[mon.lower()]
        ident = f'{int(y):04d}-{mo:02d}-{int(d):02d}-{int(nr)}'
        return f'{"LOV" if kind.lower() == "lov" else "FOR"}-{ident}'
    return fallback


def parse_act(path: pathlib.Path) -> list[dict]:
    doc = lxml.html.fromstring(path.read_bytes(),
                               parser=lxml.html.HTMLParser(encoding='utf-8'))
    meta = {}
    for dd in doc.xpath('//dl[contains(@class,"data-document-key-info")]/dd'):
        k = (dd.get('class') or '').split()
        if k:
            meta.setdefault(k[0], canon(dd.text_content()))
            if k[0] == 'changesToDocuments':
                refs = [(a.get('href') or '').lstrip('/') for a in dd.findall('.//a')]
                if not refs:
                    refs = [canon(li.text_content()).lstrip('/') for li in dd.findall('.//li')] \
                        or [canon(dd.text_content()).lstrip('/')]
                meta['targets'] = [r for r in refs if r]
    act_id = meta.get('legacyID')
    if not act_id:
        return []
    targets = meta.get('targets') or []
    sole_target = work_id_from_ref(targets[0]) if len(targets) == 1 else None
    act_in_force = meta.get('dateInForce') or None
    if act_in_force:
        m = re.search(r'\d{4}-\d{2}-\d{2}', act_in_force)
        act_in_force = m.group(0) if m else None

    body = doc.xpath('//*[contains(@class,"documentBody")]')
    if not body:
        return []
    parts = body[0].xpath('./*[contains(@class,"section")]') or [body[0]]

    # Entry into force may be stated in its own trailing part; that date then
    # applies to the whole act unless a part states its own.
    global_in_force = None
    for p in parts:
        t = canon(p.text_content())
        if IN_FORCE.search(t) and not PARAGRAF.search(t):
            global_in_force = nor_date(t) or global_in_force

    out: list[dict] = []
    for part in parts:
        label = canon(part.findtext('./h2') or part.findtext('./h3') or '')
        ptext = canon(part.text_content())
        work = part_target_work(ptext, sole_target)
        if not work:
            continue
        work_ref = ('lov/' if work.startswith('LOV') else 'forskrift/') + work.split('-', 1)[1]
        part_in_force = (nor_date(ptext) if IN_FORCE.search(ptext) else None) \
            or global_in_force or act_in_force

        instrs = [canon(c.text_content()) for c in part.iter() if is_instruction(c)]
        # Drop duplicates that arise when an instruction element nests inside
        # another matching element, keeping document order.
        seen_i, ordered = set(), []
        for t in instrs:
            if t not in seen_i:
                seen_i.add(t)
                ordered.append(t)
        whole = canon(part.text_content())
        spans, cursor = [], 0
        for t in ordered:                      # locate each instruction in order
            at = whole.find(t, cursor)
            if at < 0:
                continue
            spans.append((at, at + len(t), t))
            cursor = at + len(t)

        for j, (start, stop, instr) in enumerate(spans):
            op = instruction_op(instr)
            key, sub = target_key(instr, work_ref)
            if not key and not NON_PARAGRAF.search(instr):
                continue                       # not an amendment we can place
            renamed_to = None
            if op == 'renumber':
                rm = RENUM.search(instr)
                after = PARAGRAF.search(instr[rm.end():]) if rm else None
                renamed_to = f'{work_ref}/§{after.group(1)}' if after else None
            end = spans[j + 1][0] if j + 1 < len(spans) else len(whole)
            new_text = canon(whole[stop:end])
            if op in ('amend', 'insert') and not new_text:
                continue                      # instruction with no wording after it
            if op in ('repeal', 'renumber'):
                new_text = ''                 # these state no wording, by nature
            out.append(dict(
                act=act_id, part=label or None, target_work=work,
                target_key=key, sub_target=sub, operation=op,
                # Amendments to annexes, headings and EEA fields have no § to
                # attach to; they are recorded rather than dropped.
                target_kind='provision' if key else 'other',
                renamed_to=renamed_to,
                in_force_on=part_in_force,
                new_text=new_text or None,
                new_text_sha256=sha256(new_text) if new_text else None,
                instruction=instr[:220], source_file=path.name,
                confidence=1.0 if (part_in_force and new_text) else 0.6,
            ))
    return out


def main(root: str, out: str) -> None:
    files = sorted(pathlib.Path(root).rglob('*.xml'))
    n, stats = 0, collections.Counter()
    with open(out, 'w', encoding='utf-8') as fh:
        for i, f in enumerate(files, 1):
            try:
                recs = parse_act(f)
            except Exception as exc:                      # noqa: BLE001
                stats['failed'] += 1
                continue
            if recs:
                stats['acts_with_amendments'] += 1
            for r in recs:
                fh.write(json.dumps(r, ensure_ascii=False) + '\n')
                n += 1
                stats[r['operation']] += 1
                if r['in_force_on']:
                    stats['dated'] += 1
            if i % 5000 == 0:
                print(f'  ...{i}/{len(files)}  {n:,} amendments', flush=True)
    print(json.dumps({'amendments': n, **stats}, indent=1))


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
