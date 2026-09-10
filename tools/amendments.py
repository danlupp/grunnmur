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


BLOCK_CLASSES = {'defaultP', 'legalP', 'listArticle', 'numberedLegalP',
                 'futureLegalArticle', 'miscHeadline', 'legalArticle'}
SUBLEVEL = re.compile(r'/(ledd|punkt|punktum|bokstav)/')


def _cls(el) -> set[str]:
    return set((el.get('class') or '').split())


def top_level_blocks(part) -> list:
    """The part's content as a flat, document-order sequence of blocks.

    Outermost blocks, EXCEPT that a block hiding instructions inside it is
    descended into rather than taken whole -- instructions nest inside lists,
    tables and subsections in 16 % of acts, and taking the outer block would
    swallow both the instruction and the wording it introduces.
    """
    out: list = []

    def walk(el):
        for child in el:
            if _cls(child) & BLOCK_CLASSES:
                if any(is_instruction(d) for d in child.iterdescendants()):
                    walk(child)          # instructions inside: go deeper
                else:
                    out.append(child)
            else:
                walk(child)              # not a block (ul, td, div): pass through

    walk(part)
    return out


def ledd_blocks(content: list) -> list:
    """The ledd of a replacement paragraph, in order.

    A replacement may arrive as bare legalP siblings, or wrapped in a single
    futureLegalArticle/legalArticle whose legalP children are the ledd.
    """
    direct = [b for b in content if 'legalP' in _cls(b)]
    if direct:
        return direct
    if len(content) == 1 and _cls(content[0]) & {'futureLegalArticle', 'legalArticle'}:
        return [k for k in content[0].iterdescendants() if 'legalP' in _cls(k)
                and not any('legalP' in _cls(a) for a in k.iterancestors()
                            if a is not content[0])]
    return []


def punkt_blocks(ledd) -> list:
    """Lettered/numbered list items directly under one ledd."""
    return [k for k in ledd.iterdescendants() if 'listArticle' in _cls(k)
            and not any('listArticle' in _cls(a) for a in k.iterancestors())]


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
    # "§ 13 tredje ledd blir nytt fjerde ledd" names TWO positions; only the
    # first is the target. Stop at the renumbering verb, and take one ledd only.
    rm = RENUM.search(tail)
    if rm:
        tail = tail[:rm.start()]
    seen_ledd = False
    for m in SUBDIV.finditer(tail):
        n = ORDINALS.get((m.group(2) or '').lower()) or (int(m.group(3)) if m.group(3) else None)
        if not n:
            continue
        level = m.group(4).lower()
        if level == 'punktum':          # a sentence: finer than we model
            sub = f'punktum/{n}'
            break
        if level == 'ledd':
            if seen_ledd:
                break
            seen_ledd = True
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

        blocks = top_level_blocks(part)
        idx = [i for i, b in enumerate(blocks) if is_instruction(b)]

        for j, i in enumerate(idx):
            instr = canon(blocks[i].text_content())
            op = instruction_op(instr)
            key, sub = target_key(instr, work_ref)
            if not key and not NON_PARAGRAF.search(instr):
                continue                       # not an amendment we can place
            renamed_to = None
            if op == 'renumber':
                rm = RENUM.search(instr)
                rest = instr[rm.end():] if rm else ''
                after = PARAGRAF.search(rest)
                if after:
                    renamed_to = f'{work_ref}/§{after.group(1)}'
                elif key:
                    # a ledd moved within its §: "tredje ledd blir nytt fjerde ledd"
                    sm = SUBDIV.search(rest)
                    n = (ORDINALS.get((sm.group(2) or '').lower())
                         or (int(sm.group(3)) if sm and sm.group(3) else None)) if sm else None
                    if n and sm.group(4).lower() == 'ledd':
                        renamed_to = f"{key.split('/ledd/')[0]}/ledd/{n}"
            stop = idx[j + 1] if j + 1 < len(idx) else len(blocks)
            content = blocks[i + 1:stop]
            new_text = canon(' '.join(b.text_content() for b in content))
            if op in ('amend', 'insert') and not new_text:
                continue                      # instruction with no wording after it
            if op in ('repeal', 'renumber'):
                new_text = ''                 # these state no wording, by nature

            def rec(k, text, level='paragraph', sub_t=sub):
                return dict(
                    act=act_id, part=label or None, target_work=work,
                    target_key=k, sub_target=sub_t, operation=op,
                    # Amendments to annexes, headings and EEA fields have no § to
                    # attach to; they are recorded rather than dropped.
                    target_kind='provision' if k else 'other',
                    renamed_to=renamed_to, in_force_on=part_in_force,
                    new_text=text or None,
                    new_text_sha256=sha256(text) if text else None,
                    derived=level,
                    instruction=instr[:220], source_file=path.name,
                    confidence=1.0 if (part_in_force and text) else 0.6,
                )

            out.append(rec(key, new_text))

            # A whole-paragraph replacement CONTAINS its own sub-structure: the
            # ledd of the new § arrive as separate legalP blocks. Emitting them
            # individually turns a paragraph-level blob into exact ledd-level
            # wording -- structure recovered from evidence, not inferred.
            if key and op in ('amend', 'insert') and not SUBLEVEL.search(key):
                for n, ledd in enumerate(ledd_blocks(content), 1):
                    ltext = canon(ledd.text_content())
                    if not ltext:
                        continue
                    lkey = f'{key}/ledd/{n}'
                    out.append(rec(lkey, ltext, 'ledd', None))
                    for m, pt in enumerate(punkt_blocks(ledd), 1):
                        ptext_ = canon(pt.text_content())
                        if ptext_:
                            out.append(rec(f'{lkey}/punkt/{m}', ptext_, 'punkt', None))
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
