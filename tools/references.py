#!/usr/bin/env python3
"""Extract the citation graph from Lovdata markup.

Lovdata already marks cross-references as links carrying a provision-level
target (`lov/2016-06-17-73/§5a`), so this is near-verbatim extraction rather
than prose parsing -- which is why it is kept separate from the amendment
grammar and carries much higher confidence.

Two classes of link are deliberately NOT citations and are excluded:

  * links inside `article.changesToParent` -- those are change provenance,
    already modelled as change events and `amends` edges;
  * links in the document header (`basedOn`, `changesToDocuments`) -- already
    modelled as `hasLegalBasis` and `amends`.

❗ Reference style. Norwegian legal drafting is overwhelmingly DYNAMIC: "jf.
forvaltningsloven § 2" means § 2 as in force when the citing rule is applied,
not as it stood when the citation was written. Static references are the rare,
explicitly-marked case ("slik den lød", "som gjaldt da"). Getting this backwards
silently returns the wrong text, so the default is dynamic and static requires a
textual cue.

Usage: python3 tools/references.py <unpacked-dir> <out.jsonl>
"""
from __future__ import annotations
import re, sys, json, pathlib, collections
import lxml.html

from lovdata import canon, work_id_from_ref, strip_collection, classes, STRUCTURAL

# A reference to a point in time, rather than to the rule as it stands.
STATIC_CUE = re.compile(
    r'slik\s+(den|de|det)\s+l(ø|y)d|som\s+gjaldt|som\s+lød|'
    r'i\s+sin\s+opprinnelige|før\s+endring', re.I)
# EU/EEA instruments are a different relation from a citation to Norwegian law.
EEA_PREFIX = re.compile(r'^(eu|avtale|eos|eøs)/', re.I)
DOC_REF = re.compile(r'^(lov|forskrift|vedtak|avtale|eu|grunnlovsvedtak)/', re.I)


def extract(path: pathlib.Path) -> list[dict]:
    doc = lxml.html.fromstring(path.read_bytes(),
                               parser=lxml.html.HTMLParser(encoding='utf-8'))
    meta = {}
    for dd in doc.xpath('//dl[contains(@class,"data-document-key-info")]/dd'):
        k = (dd.get('class') or '').split()
        if k:
            meta.setdefault(k[0], canon(dd.text_content()))
    work_id = meta.get('legacyID')
    if not work_id:
        return []
    raw_lang = (doc.get('lang') or '').split('-')[0].strip().lower()
    language = raw_lang if re.fullmatch(r'[a-z]{2}', raw_lang) else 'nb'
    language = 'nb' if language == 'no' else language

    body = doc.xpath('//*[contains(@class,"documentBody")]')
    if not body:
        return []

    out: list[dict] = []

    def walk(el, anc_key: str | None, anc_id: str | None, cur_key: str | None):
        cls = classes(el)
        # change provenance, not citation
        if 'changesToParent' in cls:
            return
        if cls & STRUCTURAL:
            url, eid = el.get('data-lovdata-url'), el.get('id')
            if url:
                cur_key = anc_key = strip_collection(url)
                anc_id = eid
            elif eid and anc_key and anc_id and eid.startswith(anc_id + '-'):
                cur_key = anc_key + '/' + eid[len(anc_id) + 1:].replace('-', '/')
            elif eid and anc_key:
                cur_key = anc_key + '/' + eid.replace('-', '/')

        if el.tag == 'a':
            href = (el.get('href') or '').strip().lstrip('/')
            if DOC_REF.match(href):
                # context = the sentence the link sits in, for the style cue
                parent = el.getparent()
                context = canon(parent.text_content())[:400] if parent is not None else ''
                dst_work = work_id_from_ref(href)
                dst_key = strip_collection(href.split('#')[0])
                is_eea = bool(EEA_PREFIX.match(href))
                # a target with no /§ or similar suffix is a whole-work citation
                provision_level = dst_key.count('/') >= 2
                out.append(dict(
                    kind='implementsEEA' if is_eea else 'cites',
                    src_work=work_id, language=language, src_provision=cur_key,
                    dst_work=dst_work,
                    dst_provision=dst_key if (provision_level and dst_work) else None,
                    dst_raw=href,
                    internal=bool(dst_work) and dst_work == work_id,
                    ref_style='static' if STATIC_CUE.search(context) else 'dynamic',
                    link_text=canon(el.text_content())[:80],
                    confidence=1.0 if dst_work else 0.5,
                    source_file=path.name,
                ))
            return                     # links do not contain provisions

        for child in el:
            walk(child, anc_key, anc_id, cur_key)

    walk(body[0], None, None, None)
    return out


def main(root: str, out: str) -> None:
    files = sorted(pathlib.Path(root).rglob('*.xml'))
    stats = collections.Counter()
    n = 0
    with open(out, 'w', encoding='utf-8') as fh:
        for i, f in enumerate(files, 1):
            try:
                recs = extract(f)
            except Exception:                                  # noqa: BLE001
                stats['failed'] += 1
                continue
            for r in recs:
                fh.write(json.dumps(r, ensure_ascii=False) + '\n')
                n += 1
                stats[r['kind']] += 1
                stats['internal' if r['internal'] else 'cross_document'] += 1
                stats['provision_level' if r['dst_provision'] else 'work_level'] += 1
                if r['ref_style'] == 'static':
                    stats['static_style'] += 1
            if i % 10000 == 0:
                print(f'  ...{i}/{len(files)}  {n:,} references', flush=True)
    print(json.dumps({'references': n, **stats}, indent=1))


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
