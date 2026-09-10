#!/usr/bin/env python3
"""Parse an unpacked Lovdata slice into normalised records.

Emits four JSONL streams: works, provisions, change events and edges.
Interprets only what the markup states; every derived claim carries a
confidence and the literal source string that justifies it.

Provision identity uses Lovdata's own `data-lovdata-URL` / `data-change-part`
idiom (e.g. `lov/2015-04-10-17/§20-1/ledd/1/bokstav/a`) rather than a derived
key, so our identifiers and Lovdata's agree by construction.
"""
from __future__ import annotations
import re, json, hashlib, pathlib, unicodedata
import lxml.html

# ---------------------------------------------------------------- dates

MONTHS = {
    'jan': 1, 'januar': 1, 'feb': 2, 'februar': 2, 'mar': 3, 'mars': 3,
    'apr': 4, 'april': 4, 'mai': 5, 'jun': 6, 'juni': 6, 'jul': 7, 'juli': 7,
    'aug': 8, 'august': 8, 'sep': 9, 'sept': 9, 'september': 9,
    'okt': 10, 'oktober': 10, 'nov': 11, 'november': 11, 'des': 12, 'desember': 12,
}
_MONTH_ALT = '|'.join(sorted(MONTHS, key=len, reverse=True))
# "1 jan 2006", "1. januar 2006"
NOR_DATE = re.compile(rf'\b(\d{{1,2}})\.?\s+({_MONTH_ALT})\.?\s+(\d{{4}})\b', re.I)
ISO_DATE = re.compile(r'\b(\d{4})-(\d{2})-(\d{2})\b')


def nor_date(text: str) -> str | None:
    """First Norwegian-form date in `text`, as ISO. None if absent."""
    m = NOR_DATE.search(text)
    if not m:
        return None
    d, mon, y = int(m.group(1)), MONTHS[m.group(2).lower()], int(m.group(3))
    try:
        from datetime import date
        return date(y, mon, d).isoformat()
    except ValueError:
        return None


def iso_date(text: str) -> str | None:
    m = ISO_DATE.search(text or '')
    return f'{m.group(1)}-{m.group(2)}-{m.group(3)}' if m else None


# ------------------------------------------------------- canonicalisation

_WS = re.compile(r'\s+')
_STRIP = dict.fromkeys(map(ord, '­​‌‍﻿'), None)
CANON_VERSION = 1


def canon(text: str) -> str:
    """Deterministic text canonicalisation. Bump CANON_VERSION when changed."""
    t = unicodedata.normalize('NFC', text or '')
    t = t.translate(_STRIP).replace(' ', ' ')
    t = (t.replace('‘', "'").replace('’', "'")
          .replace('“', '"').replace('”', '"')
          .replace('–', '-').replace('—', '-'))
    return _WS.sub(' ', t).strip()


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


# --------------------------------------------------------------- identity

# Lovdata spells a document kind one way in hrefs and another in legacyID.
HREF_KIND_TO_LEGACY = {
    'lov': 'LOV', 'forskrift': 'FOR', 'vedtak': 'FOR',
    'kgl.res': 'FOR', 'res': 'FOR', 'grunnlovsvedtak': 'LOV', 'avtale': 'AVT',
}

DOC_HREF = re.compile(r'^(lov|forskrift|vedtak|avtale|kgl\.res|res|grunnlovsvedtak)/([\w.\-§/]+)$', re.I)


def work_id_from_ref(ref: str) -> str | None:
    """'lov/2016-06-17-73' -> 'LOV-2016-06-17-73'. Provision suffixes dropped."""
    if not ref:
        return None
    ref = ref.split('#')[0].strip().lstrip('/')
    for prefix in ('NL/', 'SF/', 'LTI/'):
        if ref.startswith(prefix):
            ref = ref[len(prefix):]
    parts = ref.split('/')
    if len(parts) < 2:
        return None
    kind, ident = parts[0], parts[1]
    if not re.match(r'^\d{4}-\d{2}-\d{2}-\d+$', ident):
        return None
    return f'{HREF_KIND_TO_LEGACY.get(kind.lower(), kind.upper())}-{ident}'


def strip_collection(url: str) -> str:
    """'NL/lov/2016-06-17-73/§1' -> 'lov/2016-06-17-73/§1' (data-change-part idiom)."""
    for prefix in ('NL/', 'SF/', 'LTI/'):
        if url.startswith(prefix):
            return url[len(prefix):]
    return url


# ---------------------------------------------------- change annotations

VERBS = [
    ('Opphev',  'repeal'), ('Oppheva', 'repeal'),
    ('Tilføy',  'insert'), ('Tilføy',  'insert'),
    ('Endre',   'amend'),  ('Endra',   'amend'), ('Endret', 'amend'),
    ('Kapitteloverskrift', 'amend'), ('Overskrift', 'amend'),
]
# Present tense marks a change annotated but NOT yet in force.
FUTURE_TENSE = re.compile(r'^\s*(?:\w+\s+)?(Endres|Tilføyes|Oppheves)\b')


SEG_VERB = re.compile(
    r'\b(opphev\w*|oppheva\w*|tilf\w*y\w*|endr\w*|erstatt\w*)\s+(?:ved|av|igjen\s+ved)\b', re.I)


def _verb_to_op(word: str) -> str:
    w = word.lower()
    if w.startswith(('opphev', 'oppheva')):
        return 'repeal'
    if w.startswith('tilf'):
        return 'insert'
    return 'amend'


def classify_verb(text: str) -> tuple[str, bool]:
    """(operation, is_future) from the annotation's leading verb."""
    head = text.strip()[:60]
    future = bool(FUTURE_TENSE.match(head))
    m = SEG_VERB.search(head)
    if m:
        return _verb_to_op(m.group(1)), future
    for prefix, op in VERBS:
        if re.search(rf'\b{prefix}', head):
            return op, future
    return 'amend', future


def segment_op(prev_tail: str, default: str) -> str:
    """Operation for one source, from the connector text introducing its link.

    'Tilføyd ved forskrift X, opphevet ved forskrift Y' -> insert, then repeal.
    """
    ms = list(SEG_VERB.finditer(prev_tail or ''))
    return _verb_to_op(ms[-1].group(1)) if ms else default


IN_FORCE_CUE = re.compile(r'\bi\s*kraft\b|\bikr\.?\b|\bi\.?\s*kr\.?\b', re.I)
# Valid time is also expressed as an effect clause, especially in fiscal law.
EFFECT_CUE = re.compile(r'\bf\.?o\.?m\.?\b|\bmed\s+virkning\b|\bgjelder\s+fra\b|\bfra\s+og\s+med\b', re.I)
EFFECT_YEAR = re.compile(r'\b(?:skatte|inntekts|regnskaps|termin)?året?\s+(\d{4})\b', re.I)


def in_force_from(clause: str) -> tuple[str | None, str | None]:
    """(iso date or None, unresolved-reason or None) from an entry-into-force clause."""
    if not IN_FORCE_CUE.search(clause):
        if EFFECT_CUE.search(clause):
            # "(fom inntektsåret 2005)" -> valid from the start of that year.
            d = nor_date(clause)
            if not d:
                y = EFFECT_YEAR.search(clause)
                d = f'{y.group(1)}-01-01' if y else None
            return d, 'virkningstidspunkt'
        # A repeal annotation often dates itself without an 'i kraft' cue.
        return (nor_date(clause), None) if re.search(r'Opphev', clause, re.I) else (None, None)
    if 'Kongen bestemmer' in clause:
        return None, 'kongen_bestemmer'
    if 'departementet bestemmer' in clause.lower():
        return None, 'departementet_bestemmer'
    if 'straks' in clause:
        return None, 'straks'
    return nor_date(clause), None


_L, _M, _R = '\x00', '\x01', '\x02'


def _mark_links(el) -> str:
    """Annotation text with document links wrapped as \x00href\x01label\x02."""
    out: list[str] = []

    def rec(node, is_root=False):
        if node.tag == 'a' and not is_root:
            href = (node.get('href') or '').strip().lstrip('/')
            if DOC_HREF.match(href):
                out.append(f'{_L}{href}{_M}{node.text_content()}{_R}')
            else:
                out.append(node.text_content())
        else:
            if node.text:
                out.append(node.text)
            for child in node:
                rec(child)
        if not is_root and node.tail:
            out.append(node.tail)

    rec(el, is_root=True)
    return ''.join(out)


def parse_changes(article, work_id: str, provision_key: str, source_file: str) -> list[dict]:
    """One `article.changesToParent` -> zero or more change events.

    The changing work is taken from the <a href> Lovdata already provides,
    so only the entry-into-force clause needs grammar parsing.
    """
    raw = canon(article.text_content())
    operation, future = classify_verb(raw)
    events: list[dict] = []

    # Serialise the annotation with sentinels around document links, so each
    # link's trailing clause is bounded by the next link rather than running on.
    marked = _mark_links(article)
    parts = re.split(r'\x00([^\x01]*)\x01([^\x02]*)\x02', marked)
    # parts = [pre, href, label, tail, href, label, tail, ...]
    pre, segments = parts[0], [
        (parts[i], parts[i + 1], parts[i + 2]) for i in range(1, len(parts) - 2, 3)
    ]

    if not segments:  # annotation with no link, e.g. "Opphevet 11 feb 2011, jf. ..."
        d = nor_date(raw)
        if d or re.search(r'Opphev', raw, re.I):
            events.append(dict(changing_work=None, in_force_on=d, in_force_note=None))
    else:
        prev_tail = pre
        for href, _label, tail in segments:
            cw = work_id_from_ref(href)
            if cw is None:
                prev_tail = tail
                continue
            # A link introduced by 'iflg.'/'jf.' is the authority that SET the
            # date, not a separate amendment - attach it to the preceding event.
            if re.search(r'(iflg\.?|jf\.?|etter)\s*$', canon(prev_tail)) and events:
                events[-1]['in_force_source'] = cw
                events[-1]['_tail'] = events[-1].get('_tail', '') + ' ' + tail
                prev_tail = tail
                continue
            d, note = in_force_from(canon(tail))
            events.append(dict(changing_work=cw, in_force_on=d, in_force_note=note,
                               operation=segment_op(canon(prev_tail), operation),
                               _tail=tail))
            prev_tail = tail

    for ev in events:  # renumbering is marked in the clause, e.g. "(tidligere § 27-6)"
        if 'tidligere' in canon(ev.get('_tail', '')):
            ev['renumbered_from'] = canon(ev['_tail'])[:200]

    out = []
    for ev in events:
        if not ev.get('in_force_on') and ev.get('changing_work'):
            m = re.search(r'(\d{4}-\d{2}-\d{2})', ev['changing_work'])
            ev['in_force_earliest'] = m.group(1) if m else None
        tail = ev.pop('_tail', '')
        op = 'renumber' if ev.get('renumbered_from') else ev.get('operation', operation)
        out.append(dict(
            changed_work=work_id,
            changed_provision=provision_key,
            changing_work=ev.get('changing_work'),
            operation=op,
            in_force_on=ev.get('in_force_on'),
            # Lower bound only, for events with no stated date. Never a fact.
            in_force_earliest=ev.get('in_force_earliest'),
            in_force_note=ev.get('in_force_note'),
            in_force_source=ev.get('in_force_source'),
            state='pending' if (future or ev.get('in_force_note')) else 'applied',
            future_tense=future,
            method='changesToParent',
            # A parse is confident when it produced both a changing work and a date.
            confidence=(1.0 if (ev.get('changing_work') and ev.get('in_force_on'))
                        else 0.8 if ev.get('in_force_note')
                        else 0.4 if ev.get('in_force_earliest') else 0.2),
            raw=raw[:500],
            source_file=source_file,
        ))
    return out


# ------------------------------------------------------------- documents

STRUCTURAL = {'legalArticle', 'legalP', 'listArticle', 'numberedLegalP', 'section', 'chapter'}


def classes(el) -> set[str]:
    return set((el.get('class') or '').split())


def parse_document(path: pathlib.Path) -> dict:
    parser = lxml.html.HTMLParser(encoding='utf-8')
    doc = lxml.html.fromstring(path.read_bytes(), parser=parser)
    meta: dict[str, object] = {}
    for dd in doc.xpath('//dl[contains(@class,"data-document-key-info")]/dd'):
        key = (dd.get('class') or '').split()[0] if dd.get('class') else None
        if not key:
            continue
        meta.setdefault(key, canon(dd.text_content()))
        if key in ('basedOn', 'changesToDocuments', 'lastChangedBy'):
            meta.setdefault(key + '_refs', [])
            meta[key + '_refs'].extend(  # type: ignore[union-attr]
                (a.get('href') or '').strip().lstrip('/') for a in dd.findall('.//a'))

    work_id = meta.get('legacyID') or work_id_from_ref(str(meta.get('refid', '')))
    if not work_id:
        return {}
    work_id = str(work_id)
    # Grunnloven ships as bokmål AND nynorsk under one work id with identical
    # Lovdata keys, so language is ours to carry. ELI models it the same way.
    raw_lang = (doc.get('lang') or '').split('-')[0].strip().lower()
    # Lovdata writes 'no' for most documents, 'nb'/'nn' for some; one document's
    # root attribute is malformed, so anything that is not a bare 2-letter code
    # falls back to bokmål rather than becoming a bogus language.
    language = raw_lang if re.fullmatch(r'[a-z]{2}', raw_lang) else 'nb'
    language = 'nb' if language == 'no' else language
    doc_type = 'lov' if work_id.startswith('LOV') else (
        'sentral_forskrift' if work_id.startswith('FOR') else 'stortingsvedtak')

    work = dict(
        work_id=work_id, doc_type=doc_type,
        title=meta.get('title'), short_title=meta.get('titleShort'),
        ministry=meta.get('ministry'),
        date_in_force=iso_date(str(meta.get('dateInForce', ''))),
        published_on=iso_date(str(meta.get('dateOfPublication', ''))),
        last_change_in_force=iso_date(str(meta.get('lastChangeInForce', ''))),
        last_corrected=iso_date(str(meta.get('lastupdated', ''))),
        legal_area=meta.get('legalArea'), language=language, source_file=path.name,
    )

    provisions, events, edges = [], [], []

    # hjemmel: regulation -> the statutory provision empowering it
    for ref in dict.fromkeys(meta.get('basedOn_refs', [])):      # type: ignore[arg-type]
        dst = work_id_from_ref(ref)
        if dst:
            edges.append(dict(kind='hasLegalBasis', src_work=work_id, dst_work=dst,
                              dst_provision=strip_collection(ref), confidence=1.0,
                              method='basedOn'))
    for ref in dict.fromkeys(meta.get('changesToDocuments_refs', [])):  # type: ignore[arg-type]
        dst = work_id_from_ref(ref)
        if dst:
            edges.append(dict(kind='amends', src_work=work_id, dst_work=dst,
                              dst_provision=None, confidence=1.0, method='changesToDocuments'))

    # Walk the structural tree, carrying (key, id) of the nearest keyed ancestor.
    def walk(el, anc_key: str | None, anc_id: str | None, parent_key: str | None,
             depth: int, url_chain: tuple[str, ...] = ()):
        cls = classes(el)
        key, own_id = parent_key, anc_id
        if cls & STRUCTURAL:
            url = el.get('data-lovdata-url')
            eid = el.get('id')
            if url:
                key, anc_key, anc_id = strip_collection(url), strip_collection(url), eid
                url_chain = url_chain + (key.rsplit('/', 1)[-1],)
            elif eid and anc_key and anc_id and eid.startswith(anc_id + '-'):
                # 'paragraf-2-ledd-2-punkt-1' under 'paragraf-2' -> '.../ledd/2/punkt/1'
                key = anc_key + '/' + eid[len(anc_id) + 1:].replace('-', '/')
            elif eid and anc_key:
                key = anc_key + '/' + eid.replace('-', '/')
            if key:
                own_text = canon(''.join(el.itertext()))
                repealed = el.get('data-repealeddate')
                provisions.append(dict(
                    work_id=work_id, language=language, logical_key=key, parent_key=parent_key,
                    level=('paragraf' if 'legalArticle' in cls else
                           'ledd' if 'legalP' in cls else
                           'punkt' if 'listArticle' in cls else 'seksjon'),
                    designator=el.get('data-name'),
                    heading=canon(el.findtext('.//span[@class="legalArticleTitle"]') or ''),
                    element_id=el.get('id'), depth=depth,
                    _chain=url_chain[:-1], _own=key.rsplit('/', 1)[-1],
                    repealed_on=repealed,
                    text_sha256=sha256(own_text), text=own_text,
                    canon_version=CANON_VERSION,
                ))
                parent_key = key
        for child in el:
            if 'changesToParent' in classes(child):
                events.extend(parse_changes(child, work_id, parent_key, path.name))
            else:
                walk(child, anc_key, anc_id, parent_key, depth + 1, url_chain)

    body = doc.xpath('//*[contains(@class,"documentBody")]') or [doc]
    walk(body[0], None, None, None, 0)

    # Lovdata's data-lovdata-URL is stable but NOT unique within a document: a
    # forskrift reproducing a convention and its protocol gives "Art I" the same
    # URL twice. Disambiguate collisions with the nearest ancestor's own Lovdata
    # URL tail (stable), walking up until unique; the element anchor is the last
    # resort and is flagged, because anchors are positional and shift on insert.
    by_key: dict[str, list[dict]] = {}
    for pr in provisions:
        by_key.setdefault(pr['logical_key'], []).append(pr)
    remap: dict[str, str] = {}
    for key, group in by_key.items():
        if len(group) < 2:
            continue
        prefix = key.rsplit('/', 1)[0]
        for pr in group:
            chain, new = pr['_chain'], None
            for depth_up in range(1, len(chain) + 1):
                cand = '/'.join((prefix, *chain[-depth_up:], pr['_own']))
                if sum(1 for q in group
                       if '/'.join((prefix, *q['_chain'][-depth_up:], q['_own'])) == cand) == 1:
                    new = cand
                    break
            if new is None:
                new = f"{key}@{pr['element_id']}"
                pr['identity_from_anchor'] = True
            remap[pr['logical_key'] + '\x00' + str(pr['element_id'])] = new
            pr['logical_key'] = new
    for pr in provisions:                    # re-point children at renamed parents
        for k, v in remap.items():
            if pr['parent_key'] and pr['parent_key'] == k.split('\x00')[0]:
                pass
        pr.pop('_chain', None); pr.pop('_own', None)
    # rebuild parent links from the (now unique) key set
    keys = {pr['logical_key'] for pr in provisions}
    for pr in provisions:
        if pr['parent_key'] and pr['parent_key'] not in keys:
            cands = [k for k in keys if k.endswith('/' + pr['parent_key'].rsplit('/', 1)[-1])
                     and pr['logical_key'].startswith(k.rsplit('/', 1)[0])]
            pr['parent_key'] = cands[0] if len(cands) == 1 else None
    return dict(work=work, provisions=provisions, events=events, edges=edges)


def main(root: str, outdir: str) -> None:
    out = pathlib.Path(outdir); out.mkdir(parents=True, exist_ok=True)
    files = sorted(pathlib.Path(root).rglob('*.xml'))
    handles = {n: (out / f'{n}.jsonl').open('w', encoding='utf-8')
               for n in ('works', 'provisions', 'events', 'edges')}
    counts = dict.fromkeys(handles, 0)
    failed = []
    for i, f in enumerate(files, 1):
        try:
            rec = parse_document(f)
        except Exception as exc:                     # noqa: BLE001 - recorded, not raised
            failed.append((f.name, repr(exc)[:120]))
            continue
        if not rec:
            failed.append((f.name, 'no work id'))
            continue
        handles['works'].write(json.dumps(rec['work'], ensure_ascii=False) + '\n')
        counts['works'] += 1
        for name in ('provisions', 'events', 'edges'):
            for row in rec[name]:
                handles[name].write(json.dumps(row, ensure_ascii=False) + '\n')
                counts[name] += 1
        if i % 1000 == 0:
            print(f'  ...{i}/{len(files)}', flush=True)
    for h in handles.values():
        h.close()
    print(json.dumps(counts, indent=1))
    if failed:
        print(f'FAILED {len(failed)}:')
        for n, e in failed[:10]:
            print(f'  {n}: {e}')


if __name__ == '__main__':
    import sys
    main(sys.argv[1], sys.argv[2])
