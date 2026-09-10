#!/usr/bin/env python3
"""Phase 0 reconnaissance over an unpacked Lovdata slice.

Surveys the real element vocabulary, metadata coverage and change-annotation
forms, so docs/ can be corrected against data rather than against Lovdata's
published format documentation (which describes a different shape).

Usage: python3 tools/recon.py <unpacked-dir> [--json out.json]
"""
import sys, re, json, collections, pathlib

CLASS_RE = re.compile(r'class="([^"]*)"')
ATTR_RE  = re.compile(r'\s(data-[a-zA-Z-]+)=')
DT_RE    = re.compile(r'<dd class="([a-zA-Z-]+)"[^>]*>(.*?)</dd>', re.S)
CHG_RE   = re.compile(r'<article class="changesToParent"[^>]*>(.*?)</article>', re.S)
TAG_RE   = re.compile(r'<[^>]+>')

def text(h):
    return re.sub(r'\s+', ' ', TAG_RE.sub('', h)).replace('&#xa0;', ' ').strip()

def main(root, out=None):
    files = sorted(pathlib.Path(root).rglob('*.xml'))
    st = {
        'files': len(files),
        'classes': collections.Counter(),
        'data_attrs': collections.Counter(),
        'meta_keys': collections.Counter(),
        'chg_verbs': collections.Counter(),
        'chg_inforce': collections.Counter(),
        'chg_total': 0,
        'chg_samples': [],
        'opphevet_shells': 0,
        'docs_with_chg': 0,
        'docs_with_basedOn': 0,
        'earliest_chg_year': None,
        'chg_years': collections.Counter(),
    }
    for f in files:
        h = f.read_text(encoding='utf-8', errors='replace')
        for c in CLASS_RE.findall(h):
            for part in c.split():
                st['classes'][part] += 1
        st['data_attrs'].update(ATTR_RE.findall(h))
        for k, _ in DT_RE.findall(h):
            st['meta_keys'][k] += 1
        if 'class="basedOn"' in h:
            st['docs_with_basedOn'] += 1
        st['opphevet_shells'] += len(re.findall(r'\(Opphevet[^)]*\)', h))
        chgs = CHG_RE.findall(h)
        if chgs:
            st['docs_with_chg'] += 1
        for c in chgs:
            st['chg_total'] += 1
            t = text(c)
            # leading verb, e.g. "Endret ved lov", "Tilføyd ved lov"
            m = re.match(r'^(\w+)\s+ved\s+(\w+)', t)
            st['chg_verbs'][f"{m.group(1)} ved {m.group(2)}" if m else t[:40]] += 1
            # entry-into-force clause shape
            if 'i kraft' in t:
                if re.search(r'i kraft \d', t):           st['chg_inforce']['explicit date'] += 1
                elif 'Kongen bestemmer' in t:             st['chg_inforce']['fra den tid Kongen bestemmer'] += 1
                elif 'straks' in t:                       st['chg_inforce']['straks'] += 1
                else:                                     st['chg_inforce']['other'] += 1
            else:
                st['chg_inforce']['no i kraft clause'] += 1
            for y in re.findall(r'\b(1[89]\d\d|20\d\d)\b', t):
                st['chg_years'][int(y)] += 1
            if len(st['chg_samples']) < 25:
                st['chg_samples'].append(t[:220])
    if st['chg_years']:
        st['earliest_chg_year'] = min(st['chg_years'])
    st['classes'] = dict(st['classes'].most_common(45))
    st['data_attrs'] = dict(st['data_attrs'].most_common(20))
    st['meta_keys'] = dict(st['meta_keys'].most_common(40))
    st['chg_verbs'] = dict(st['chg_verbs'].most_common(20))
    st['chg_inforce'] = dict(st['chg_inforce'])
    st['chg_years'] = dict(sorted(st['chg_years'].items()))
    if out:
        pathlib.Path(out).write_text(json.dumps(st, indent=1, ensure_ascii=False))
    return st

if __name__ == '__main__':
    root = sys.argv[1]
    out = sys.argv[sys.argv.index('--json') + 1] if '--json' in sys.argv else None
    s = main(root, out)
    print(f"files={s['files']}  changesToParent={s['chg_total']} in {s['docs_with_chg']} docs")
    print(f"docs with basedOn (hjemmel)={s['docs_with_basedOn']}  opphevet-shells={s['opphevet_shells']}")
    print(f"earliest year cited in a change annotation: {s['earliest_chg_year']}")
    print("\n-- metadata keys --");    [print(f"  {v:6d}  {k}") for k, v in s['meta_keys'].items()]
    print("\n-- change verbs --");     [print(f"  {v:6d}  {k}") for k, v in s['chg_verbs'].items()]
    print("\n-- entry-into-force --"); [print(f"  {v:6d}  {k}") for k, v in s['chg_inforce'].items()]
    print("\n-- data-* attrs --");     [print(f"  {v:6d}  {k}") for k, v in s['data_attrs'].items()]
