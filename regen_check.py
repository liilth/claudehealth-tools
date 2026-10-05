#!/usr/bin/env python3
"""regen_check.py v1.4 (2026-10-05) — Rule 11 regeneration check for the fitness knowledge base.

Audits an old → new pair of every regenerated file and prints one REGEN CHECK
block per file, to be pasted verbatim into the CHANGELOG delta. The same script
is run (a) by the coach in the review chat before presenting and (b) by the
client locally before swapping the files into project knowledge; both outputs
must be identical.

What it checks per file
  bytes      old size, new size, delta; the `bytes: N` stamp (last `bytes:` in
             the file) must equal the file size — old and new.
  hunks      normal-diff hunk headers (Na, NcM, NdM), computed line-wise with
             difflib (deterministic on every machine, no GNU diff needed).
  survival   every old line of every `c` hunk must survive: VERBATIM (still a
             substring of the new file — covers the "History —" prefix and
             pure additions), INSERT-ONLY (only text was inserted, e.g. an
             in-line strike-and-restate: `~~` markup and shifted `*`/`**`
             markers are ignored), RELABEL (a `**CURRENT [...]**`-style header
             replaced by CLOSED/HISTORY with the bracket text and the body
             kept; INSERT-ONLY is verified chunk-wise on the markup-stripped
             text, so a dropped word inside a struck phrase is a FAIL), SCHEMA-REPLACE (a schema CURRENT line — Rule 12 exception:
             field values replaced; FAIL unless the entry carries a dated
             addendum this pass), or REMOVED→CHANGELOG (the whole old line
             reappears verbatim in the new CHANGELOG = client-ordered removal).
             Anything else is a FAIL.
  deletions  a `d` hunk is a FAIL, except footer lines retired under the footer
             ruling (they must reappear verbatim in the CHANGELOG, or at least
             their `bytes:` figure must; otherwise WARN).
  regions    which entries (## N. …, table rows S91/Z2-a, § sections) the
             hunks touch, plus preamble/footer; with --touched the set is
             compared with the declared list (undeclared touched = FAIL).
  entry Δ    byte delta per touched entry, preamble and footer (sum = file Δ).
  inventory  exercise-database: entries, CURRENT/CLOSED/HISTORY blocks, schema
             vs prose CURRENT, dated addenda, load-field titles, cue headers,
             touched-without-addendum and stale "carries a CURRENT block"
             annotations. training-log: rows, S-id continuity, cell counts.
             CHANGELOG: § count, latest § date.

Usage
  python3 regen_check.py --date 2026-09-21 --old /mnt/project --new /home/claude/work \
      exercise-database.md gym-profiles.md training-log-2026-09.md
  python3 regen_check.py --date 2026-09-21 --old-suffix _old --new . exercise-database.md
  Options: --touched exercise-database.md=1,11,17  (repeatable, one per file)
           --changelog PATH   new CHANGELOG.md used to verify retired footer
                              lines and client-ordered removals (default: the
                              CHANGELOG.md in --new, if present)
           --archive archive/exercise-database-archive.md   (repeatable) lines that
                    left a live file pass as ARCHIVED when found verbatim there.
  A file present only in --new (first file of a month, a new archive) is
  checked alone: stamp + structure, reported as NEW FILE.
  --allow exercise-database.md=1826,2071   old line numbers whose
                              partial loss is a declared client-ordered removal
  CHANGELOG.md itself is checked the same way (after its fixed-point stamp is
  written) but its block is never pasted — it cannot describe its own final
  size and hunks.
  --stamps FILE…    check only (Rule 11(2)): size vs last `bytes: N` of each
                    file in --new; exit 1 on any mismatch, nothing is written.
  --restamp FILE…   rewrite the last `bytes: N` of each file in --new to its
                    fixed-point size (N counts itself); prints old → new.
                    Earlier `History —` stamps are never touched.
Hunk headers are difflib's line alignment in GNU normal-diff notation; they can
differ from `diff` by the anchoring of a blank line next to an insertion. Both
sides run this script, so the pasted and the re-run headers are identical.
Known limit: a schema CURRENT line is the one place where text may change
without a diff-level trace beyond its dated addendum (Rule 12 exception).
Exit status 0 = PASS (warnings allowed), 1 = FAIL, 2 = usage/IO error.
Standard library only.
"""

import argparse
import difflib
import os
import re
import sys

# ---------------------------------------------------------------- structure --
# Entry pattern per file (extend here for nutrition files). Fallback: `## ` headings.
ENTRY_PATTERNS = {
    'exercise-database.md': ('entry', re.compile(r'^## (\d+)\. ')),
    'gym-profiles.md': ('section', re.compile(r'^## (\d+)\. ')),
    'CHANGELOG.md': ('section', re.compile(r'^## § (\d{4}-\d{2}-\d{2})')),
}
ROW_RE = re.compile(r'^\| (S\d+|Z\d+-[a-z]) \|')
HEADING_RE = re.compile(r'^## (.+?)\s*$')
STAMP_RE = re.compile(r'bytes:\s*(\d+)')
HEADER_RE = re.compile(r'^\*\*(?P<kind>CURRENT|CLOSED|HISTORY)(?P<hdr>.*?):\*\*\s?(?P<body>.*)$', re.S)
DATE_HDR = re.compile(r'\d{4}-\d{2}-\d{2} \((Mon|Tue|Wed|Thu|Fri|Sat|Sun)\)')


def kind_and_pattern(name):
    if name in ENTRY_PATTERNS:
        return ENTRY_PATTERNS[name]
    if name.startswith('training-log-'):
        return ('row', ROW_RE)
    return ('heading', HEADING_RE)


def read_lines(path):
    with open(path, 'rb') as fh:
        raw = fh.read()
    text = raw.decode('utf-8')
    lines = text.split('\n')
    if lines and lines[-1] == '':      # trailing newline → no phantom last line
        lines = lines[:-1]
        trailing_nl = True
    else:
        trailing_nl = False
    return text, lines, len(raw), trailing_nl


def line_bytes(line):
    return len(line.encode('utf-8')) + 1


def structure(lines, name):
    """Return (kind, entries, preamble_end, footer_start) with 1-based inclusive
    line numbers. entries: list of (key, start, end)."""
    kind, pat = kind_and_pattern(name)
    starts = []
    for i, l in enumerate(lines, 1):
        m = pat.match(l)
        if m:
            starts.append((m.group(1), i))
    n = len(lines)
    # footer: last '---' line after the last entry start (ED/gym/log) …
    footer_start = None
    last_start = starts[-1][1] if starts else 0
    for i in range(n, 0, -1):
        if lines[i - 1].strip() == '---' and i > last_start:
            footer_start = i
            break
    # … or, for files without a rule (CHANGELOG), the trailing stamp block
    if footer_start is None:
        i = n
        while i > 0 and lines[i - 1].strip() == '':
            i -= 1
        j = i
        while j > 0 and (lines[j - 1].strip() == '' or STAMP_RE.search(lines[j - 1]) or lines[j - 1].startswith('History — ')):
            j -= 1
        while j < i and lines[j].strip() == '':   # footer starts at the first stamp line, not at a blank
            j += 1
        if j < i and j + 1 > last_start:
            footer_start = j + 1
    if footer_start is None:
        footer_start = n + 1
    entries = []
    for k, (key, s) in enumerate(starts):
        e = starts[k + 1][1] - 1 if k + 1 < len(starts) else footer_start - 1
        if kind == 'row':
            e = s  # one line per row
        entries.append((key, s, min(e, footer_start - 1)))
    preamble_end = starts[0][1] - 1 if starts else footer_start - 1
    return kind, entries, preamble_end, footer_start


def region_of(lineno, entries, preamble_end, footer_start):
    if lineno >= footer_start:
        return ('footer', None)
    if lineno <= preamble_end:
        return ('preamble', None)
    for key, s, e in entries:
        if s <= lineno <= e:
            return ('entry', key)
    return ('gap', None)   # lines between a row table's rows and the footer, etc.


# ------------------------------------------------------------------- diff ----
def hunks(old_lines, new_lines):
    """Normal-diff hunks: list of (tag, i1, i2, j1, j2) with 0-based half-open
    ranges (tag in a/c/d) and the GNU-style header string."""
    sm = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    out = []

    def rng(a, b):  # 1-based inclusive range text
        return str(a) if a == b else '%d,%d' % (a, b)

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            continue
        if tag == 'replace':
            hdr = '%sc%s' % (rng(i1 + 1, i2), rng(j1 + 1, j2))
            out.append(('c', i1, i2, j1, j2, hdr))
        elif tag == 'insert':
            hdr = '%da%s' % (i1, rng(j1 + 1, j2))
            out.append(('a', i1, i2, j1, j2, hdr))
        elif tag == 'delete':
            hdr = '%sd%d' % (rng(i1 + 1, i2), j1)
            out.append(('d', i1, i2, j1, j2, hdr))
    return out


# --------------------------------------------------------------- survival ----
def tokens(s):
    return [t for t in s.replace('~~', '').split() if t]


def is_subseq(seq, sup):
    it = iter(sup)
    return all(any(x == y for y in it) for x in seq)


def norm(s):
    """Content view of a line: strike/bold/italic markup removed (formatting is not content)."""
    return re.sub(r'[~*]', '', s)


def lcp(a, b):
    n = min(len(a), len(b))
    k = 0
    while k < n and a[k] == b[k]:
        k += 1
    return k


def ordered_coverage(old, new, min_chunk=12, max_gap=200):
    """Does every character of `old` survive in `new`, in order, with only insertions
    between the surviving pieces? Returns (coverage 0..1, missing fragments). A piece
    continues contiguously wherever possible; after an insertion the next piece must
    reappear as a verbatim chunk of >= min_chunk characters. A tail shorter than that
    may contain one insertion of <= max_gap characters directly after its contiguous
    part (e.g. `CUES:` → `CUES (VERBATIM STANDARD):`)."""
    i, p, covered, n = 0, 0, 0, len(old)
    missing, cur = [], ''
    while i < n:
        L = lcp(old[i:], new[p:])                         # contiguous continuation
        if L >= 3:
            i += L; p += L; covered += L
            if cur:
                missing.append(cur); cur = ''
            continue
        rem = n - i
        if rem >= min_chunk:                              # continuation after an insertion
            ahead = new.find(old[i:i + min_chunk], p)
            if ahead >= 0:
                L2 = lcp(old[i:], new[ahead:])
                i += L2; p = ahead + L2; covered += L2
                if cur:
                    missing.append(cur); cur = ''
                continue
        else:                                             # short tail: one bounded insertion allowed,
            head_len = L                                  # placed right after the contiguous part
            rest = old[i + head_len:]
            q2 = new.find(rest, p + head_len) if rest else p + head_len
            if q2 >= 0 and q2 - (p + head_len) <= max_gap:
                covered += rem; p = q2 + len(rest); i = n
                if cur:
                    missing.append(cur); cur = ''
                continue
        if L >= 1:                                        # a 1–2 char contiguous match, nothing better
            i += L; p += L; covered += L
            if cur:
                missing.append(cur); cur = ''
            continue
        cur += old[i]; i += 1
    if cur:
        missing.append(cur)
    return (covered / n if n else 1.0), missing


SCHEMA_RE = re.compile(r'^\*\*CURRENT \[[^\n]*?(?:\*\*\s?|· )Load: ')


def survival(old_line, new_text, hunk_text, window_text, changelog_text=None, old_text=None):
    """Classify how an old line survived. Returns (verdict, detail)."""
    if old_line in hunk_text:
        return 'VERBATIM', ''
    if STAMP_RE.search(old_line):                       # the same stamp line with a new bytes: figure
        norm_old = STAMP_RE.sub('bytes: N', old_line)
        for nl in hunk_text.split('\n'):
            if STAMP_RE.sub('bytes: N', nl) == norm_old:
                return 'RESTAMP', ''
    if old_line in new_text and (old_text is None or new_text.count(old_line) >= old_text.count(old_line)):
        return 'VERBATIM', 'moved'          # same number of copies still exist elsewhere
    if changelog_text and old_line.strip() and old_line in changelog_text:
        return 'REMOVED→CHANGELOG', ''
    if SCHEMA_RE.match(old_line):
        return 'SCHEMA-REPLACE', ''
    m = HEADER_RE.match(old_line)
    if m and '[' in m.group('hdr') and ']' in m.group('hdr'):
        hdr = m.group('hdr')
        bracket = hdr[hdr.index('['):hdr.rindex(']') + 1]
        body = m.group('body')
        if bracket not in new_text:
            return 'FAIL', 'bracket label lost: %r' % bracket[:80]
        for scope in (hunk_text, window_text):
            if is_subseq(tokens(body), tokens(scope)):
                return 'RELABEL', ''
        best = (0.0, [])
        for scope in (hunk_text, window_text):
            cov, miss = ordered_coverage(norm(body), norm(scope))
            if cov >= 1.0:
                return 'RELABEL', 'markup-shifted'
            best = max(best, (cov, miss), key=lambda t: t[0])
        return 'FAIL', 'relabel body lost (%.1f %% survives): %s' % (best[0] * 100, '; '.join(repr(x[:50]) for x in best[1][:3]))
    for scope in (hunk_text, window_text):
        if is_subseq(tokens(old_line), tokens(scope)):
            return 'INSERT-ONLY', ''
    best = (0.0, [])
    for scope in (hunk_text, window_text):
        cov, miss = ordered_coverage(norm(old_line), norm(scope))
        if cov >= 1.0:
            return 'INSERT-ONLY', 'markup-shifted'
        best = max(best, (cov, miss), key=lambda t: t[0])
    return 'FAIL', 'old text lost (%.1f %% survives): %s' % (best[0] * 100, '; '.join(repr(x[:50]) for x in best[1][:3]))


# -------------------------------------------------------------- inventory ----
def inventory_exercise_db(lines, entries, footer_start, date, touched_keys):
    text_by_entry = {}
    for key, s, e in entries:
        text_by_entry[key] = '\n'.join(lines[s - 1:e])
    n_entries = len(entries)
    cur_blocks = schema = closed = hist = 0
    cur_entries = []
    addenda = []
    lh = cw = 0
    stale_annot = []
    cue_bad = []
    no_addendum = []
    add_re = re.compile(r'^\*\*' + re.escape(date) + r' addendum', re.M)
    for key, body in text_by_entry.items():
        c = len(re.findall(r'^\*\*CURRENT \[', body, re.M))
        s = len(re.findall(r'^\*\*CURRENT \[[^\n]*?(?:\*\*\s?|· )Load: ', body, re.M))
        cur_blocks += c
        schema += s
        if c:
            cur_entries.append(key)
        closed += len(re.findall(r'^\*\*CLOSED ', body, re.M))
        hist += len(re.findall(r'^\*\*HISTORY — pre-schema', body, re.M))
        a = len(add_re.findall(body))
        if a:
            addenda.append((key, a))
        if 'Load history by gym' in body:
            lh += 1
        if 'Current working weights/settings by gym' in body:
            cw += 1
        if re.search(r'this entry (?:now )?carries a CURRENT block', re.sub(r'~~.*?~~', '', body, flags=re.S)) and c == 0:   # struck text is history
            stale_annot.append(key)
        for hm in re.finditer(r'^\*\*CUES[^\n]*$', body, re.M):
            if not hm.group(0).startswith('**CUES (VERBATIM STANDARD'):
                cue_bad.append(key)
        if key in touched_keys and a == 0:
            no_addendum.append(key)
    never_modify = sum(1 for l in lines[:footer_start - 1] if re.search(r'NEVER.MODIFY', l)
                       and not l.startswith('- **Supersession convention'))
    out = ['inventory: %d entries · CURRENT %d blocks / %d entries (schema %d · prose %d) · CLOSED %d · HISTORY %d · addenda %s: %d in %d entries · load fields: history %d / current %d'
           % (n_entries, cur_blocks, len(cur_entries), schema, cur_blocks - schema, closed, hist,
              date, sum(a for _, a in addenda), len(addenda), lh, cw)]
    warns = []
    if no_addendum:
        warns.append('touched without a %s addendum: %s' % (date, ', '.join(no_addendum)))
    if stale_annot:
        warns.append('"carries a CURRENT block" annotation but no CURRENT block: %s' % ', '.join(stale_annot))
    if cue_bad:
        warns.append('cue header not in the ratified form: %s' % ', '.join(sorted(set(cue_bad), key=int)))
    if never_modify:
        warns.append('NEVER MODIFY residue outside the conventions line: %d' % never_modify)
    return out, warns


def inventory_training_log(lines, entries):
    rows = [k for k, _, _ in entries]
    s_ids = sorted(int(k[1:]) for k in rows if k.startswith('S'))
    z_ids = [k for k in rows if not k.startswith('S')]
    warns = []
    if s_ids:
        gaps = [x for x in range(s_ids[0], s_ids[-1] + 1) if x not in s_ids]
        if gaps:
            warns.append('S-id gaps: %s' % ', '.join('S%d' % g for g in gaps))
    cells = {}
    for key, s, _ in entries:
        cells.setdefault(lines[s - 1].count('|'), []).append(key)
        if not DATE_HDR.search(lines[s - 1]):
            warns.append('row %s without a full ISO date + weekday' % key)
    if len(cells) > 1:
        warns.append('uneven cell counts: %s' % '; '.join('%d pipes: %s' % (k, ', '.join(v)) for k, v in cells.items()))
    out = ['inventory: %d rows (%d S-rows S%d–S%d · %d Z-rows) · next session S%d'
           % (len(rows), len(s_ids), s_ids[0] if s_ids else 0, s_ids[-1] if s_ids else 0, len(z_ids), (s_ids[-1] + 1) if s_ids else 0)]
    return out, warns


def inventory_changelog(lines, entries, date):
    warns = []
    keys = [k for k, _, _ in entries]
    out = ['inventory: %d § sections (%s … %s)' % (len(keys), keys[0] if keys else '-', keys[-1] if keys else '-')]
    if keys and keys[-1] != date:
        warns.append('latest § is %s, review date is %s' % (keys[-1], date))
    return out, warns


# ------------------------------------------------------------------ check ----
def check_pair(name, old_path, new_path, date, touched_decl, changelog_text, allowed_lines, archive_text=None):
    old_text, old_lines, old_size, old_nl = read_lines(old_path)
    new_text, new_lines, new_size, new_nl = read_lines(new_path)
    fails, warns, lines_out = [], [], []
    lines_out.append('REGEN CHECK · %s · review %s' % (name, date))

    # bytes + stamps
    def stamp(text):
        m = STAMP_RE.findall(text)
        return int(m[-1]) if m else None
    so, sn = stamp(old_text), stamp(new_text)
    s_old = 'OK' if so == old_size else ('MISSING' if so is None else 'MISMATCH (stamp %s)' % so)
    s_new = 'OK' if sn == new_size else ('MISSING' if sn is None else 'MISMATCH (stamp %s)' % sn)
    if so != old_size:
        fails.append('old `bytes:` stamp %s vs size %d — Rule 11(2) stops the review' % (so, old_size))
    if sn != new_size:
        fails.append('new `bytes:` stamp %s vs size %d' % (sn, new_size))
    lines_out.append('bytes %d → %d (Δ %+d) · stamps: old %s · new %s' % (old_size, new_size, new_size - old_size, s_old, s_new))

    # structure
    o_kind, o_entries, o_pre, o_foot = structure(old_lines, name)
    n_kind, n_entries, n_pre, n_foot = structure(new_lines, name)

    # hunks
    hs = hunks(old_lines, new_lines)
    counts = {'a': 0, 'c': 0, 'd': 0}
    for h in hs:
        counts[h[0]] += 1
    lines_out.append('hunks %d (%da · %dc · %dd): %s' % (len(hs), counts['a'], counts['c'], counts['d'],
                                                         ' · '.join(h[5] for h in hs) if hs else 'none'))

    # survival of replaced lines, deletions, regions
    verdicts = {'VERBATIM': 0, 'INSERT-ONLY': 0, 'RELABEL': 0}
    c_lines = 0
    touched = {}
    retired_footer = []
    schema_replaced = set()
    for tag, i1, i2, j1, j2, hdr in hs:
        # regions in new coordinates (a/c), old coordinates (d)
        if tag == 'd':
            rng = range(i1 + 1, i2 + 1)
            for ln in rng:
                reg = region_of(ln, o_entries, o_pre, o_foot)
                touched.setdefault(reg, set()).add(hdr)
        else:
            for ln in range(j1 + 1, j2 + 1):
                reg = region_of(ln, n_entries, n_pre, n_foot)
                touched.setdefault(reg, set()).add(hdr)
        if tag == 'c':
            hunk_text = '\n'.join(new_lines[j1:j2])
            regn = region_of(j1 + 1, n_entries, n_pre, n_foot)
            if regn[0] == 'entry':
                key = regn[1]
                s_, e_ = [(s, e) for k, s, e in n_entries if k == key][0]
                window_text = '\n'.join(new_lines[s_ - 1:e_])
            else:
                window_text = '\n'.join(new_lines[max(0, j1 - 50):j2 + 50])
            for ln in range(i1, i2):
                c_lines += 1
                old_line = old_lines[ln]
                reg = region_of(ln + 1, o_entries, o_pre, o_foot)
                v, detail = survival(old_line, new_text, hunk_text, window_text, changelog_text, old_text)
                if v == 'SCHEMA-REPLACE' and reg[0] == 'entry':
                    schema_replaced.add(reg[1])
                if v == 'FAIL' and (ln + 1) in allowed_lines:
                    v, detail = 'REMOVED (declared)', ''
                if v == 'FAIL' and archive_text and old_line.strip() and old_line in archive_text:
                    v, detail = 'ARCHIVED', ''          # archive ruling 2026-10-05: the line lives verbatim in an archive file
                if v == 'FAIL':
                    if reg[0] == 'footer':
                        retired_footer.append((ln + 1, old_line))
                        verdicts.setdefault('FOOTER-RETIRED', 0)
                        verdicts['FOOTER-RETIRED'] += 1
                    else:
                        fails.append('%s old line %d not preserved — %s' % (hdr, ln + 1, detail))
                else:
                    verdicts[v] = verdicts.get(v, 0) + 1
        elif tag == 'd':
            for ln in range(i1, i2):
                reg = region_of(ln + 1, o_entries, o_pre, o_foot)
                ol = old_lines[ln]
                if reg[0] == 'footer':
                    retired_footer.append((ln + 1, ol))
                elif ol.strip() == '':
                    verdicts['blank-dropped'] = verdicts.get('blank-dropped', 0) + 1
                elif changelog_text and ol in changelog_text:
                    verdicts['REMOVED→CHANGELOG'] = verdicts.get('REMOVED→CHANGELOG', 0) + 1
                elif (ln + 1) in allowed_lines:
                    verdicts['REMOVED (declared)'] = verdicts.get('REMOVED (declared)', 0) + 1
                elif archive_text and ol in archive_text:
                    verdicts['ARCHIVED'] = verdicts.get('ARCHIVED', 0) + 1
                else:
                    fails.append('%s deletes old line %d outside the footer: %r' % (hdr, ln + 1, ol[:80]))
    n_lost = sum(1 for f in fails if 'not preserved' in f)
    lines_out.append('c-hunk survival %d/%d — %s (%s)' % (
        c_lines - n_lost, c_lines, 'FAIL' if n_lost else 'PASS',
        ' · '.join('%s %d' % (k.lower(), v) for k, v in verdicts.items() if v)))
    if retired_footer:
        found = 0
        for ln, l in retired_footer:
            if changelog_text and l.strip() and l in changelog_text:
                found += 1
            elif re.match(r'^(\*\*)?History — ', l) or re.search(r'integrity:|Byte-delta|INTEGRITY PASS|^\*\*delta:\*\*|bytes:\s*\d', l):
                found += 1          # footer ruling 2026-09-22: stamp / integrity / byte-delta lines are redundant with their review's CHANGELOG §
            else:
                m = STAMP_RE.search(l)
                n = m.group(1) if m else None
                if changelog_text and n and re.search(r'(?<![\d,])(%s|%s)(?![\d,])' % (n, format(int(n), ',')), changelog_text):
                    found += 1
                elif l.strip():
                    warns.append('retired footer line %d not found in the CHANGELOG: %r' % (ln, l[:70]))
        lines_out.append('footer lines retired %d (ruling 2026-09-22: footer keeps the current + previous stamp) · %d covered by CHANGELOG' % (
            len([1 for _, l in retired_footer if l.strip()]), found))

    # regions
    ent_keys = [k for (t, k) in touched if t == 'entry']
    def sortkey(k):
        return (0, int(k)) if k.isdigit() else (1, k)
    ent_keys = sorted(set(ent_keys), key=sortkey)
    others = [t for (t, k) in touched if t != 'entry']
    lines_out.append('regions touched: %s %s (%d)%s%s' % (
        {'entry': 'entries', 'section': 'sections', 'row': 'rows', 'heading': 'headings'}[n_kind],
        ', '.join(ent_keys) if ent_keys else 'none', len(ent_keys),
        ' · preamble' if 'preamble' in others else '', ' · footer' if 'footer' in others else ''))
    if 'gap' in others:
        fails.append('hunk outside every entry/preamble/footer region: %s' % ', '.join(sorted(touched[('gap', None)])))
    if touched_decl is not None:
        undecl = [k for k in ent_keys if k not in touched_decl]
        unused = [k for k in touched_decl if k not in ent_keys]
        if undecl:
            fails.append('touched but not declared: %s' % ', '.join(undecl))
        if unused:
            warns.append('declared but untouched: %s' % ', '.join(unused))

    # per-entry byte deltas
    def sizes(lines, entries, pre, foot, nl):
        d = {}
        for key, s, e in entries:
            d[key] = sum(line_bytes(l) for l in lines[s - 1:e])
        d['__pre__'] = sum(line_bytes(l) for l in lines[:pre])
        d['__foot__'] = sum(line_bytes(l) for l in lines[foot - 1:]) - (0 if nl else 1)
        return d
    so_, sn_ = sizes(old_lines, o_entries, o_pre, o_foot, old_nl), sizes(new_lines, n_entries, n_pre, n_foot, new_nl)
    deltas = []
    for key in sorted(set(so_) | set(sn_), key=lambda k: (2, k) if k.startswith('__') else sortkey(k)):
        if key.startswith('__'):
            continue
        dlt = sn_.get(key, 0) - so_.get(key, 0)
        if dlt:
            tag = '' if key in so_ and key in sn_ else (' (new)' if key not in so_ else ' (removed)')
            deltas.append('%s %+d%s' % (key, dlt, tag))
    deltas.append('preamble %+d' % (sn_['__pre__'] - so_['__pre__']))
    deltas.append('footer %+d' % (sn_['__foot__'] - so_['__foot__']))
    lines_out.append('entry Δ: %s' % ' · '.join(deltas))
    total = sum(sn_.values()) - sum(so_.values())
    if total != new_size - old_size:
        warns.append('entry deltas sum to %+d, file delta is %+d (structure parse mismatch)' % (total, new_size - old_size))

    # inventory
    if name == 'exercise-database.md':
        inv, w = inventory_exercise_db(new_lines, n_entries, n_foot, date, set(ent_keys))
        add_re = re.compile(r'^\*\*' + re.escape(date) + r' addendum', re.M)
        for key in sorted(schema_replaced, key=sortkey):
            s_, e_ = [(s, e) for k, s, e in n_entries if k == key][0]
            if not add_re.search('\n'.join(new_lines[s_ - 1:e_])):
                fails.append('entry %s: schema CURRENT replaced without a %s addendum (Rule 12: CURRENT may not be the sole carrier)' % (key, date))
    elif n_kind == 'row':
        inv, w = inventory_training_log(new_lines, n_entries)
    elif name == 'CHANGELOG.md':
        inv, w = inventory_changelog(new_lines, n_entries, date)
    else:
        inv, w = ['inventory: %d sections' % len(n_entries)], []
    lines_out += inv
    warns += w

    for wmsg in warns:
        lines_out.append('WARN: ' + wmsg)
    for fmsg in fails:
        lines_out.append('FAIL: ' + fmsg)
    lines_out.append('RESULT %s: %s%s' % (name, 'FAIL' if fails else 'PASS',
                                          ' (%d WARN)' % len(warns) if warns and not fails else ''))
    return lines_out, bool(fails)


def check_new(name, new_path, date):
    """A file with no old counterpart (first file of a month, a new archive): stamp + structure only."""
    new_text, new_lines, new_size, new_nl = read_lines(new_path)
    fails, lines_out = [], []
    lines_out.append('REGEN CHECK · %s · review %s · NEW FILE (no old counterpart)' % (name, date))
    m = STAMP_RE.findall(new_text)
    sn = int(m[-1]) if m else None
    s_new = 'OK' if sn == new_size else ('MISSING' if sn is None else 'MISMATCH (stamp %s)' % sn)
    if sn != new_size:
        fails.append('new `bytes:` stamp %s vs size %d' % (sn, new_size))
    lines_out.append('bytes %d · stamp %s' % (new_size, s_new))
    n_kind, n_entries, n_pre, n_foot = structure(new_lines, name)
    lines_out.append('structure: %d %s · footer %s' % (len(n_entries), {'entry': 'entries', 'section': 'sections', 'row': 'rows', 'heading': 'headings'}[n_kind], 'present' if n_foot else 'MISSING'))
    if n_kind == 'row':
        inv, w = inventory_training_log(new_lines, n_entries)
        lines_out += inv
        for wmsg in w:
            lines_out.append('WARN: ' + wmsg)
    for fmsg in fails:
        lines_out.append('FAIL: ' + fmsg)
    lines_out.append('RESULT %s: %s' % (name, 'FAIL' if fails else 'PASS'))
    return lines_out, bool(fails)


def restamp(path):
    """Rewrite the last `bytes: N` in the file to the file's own final size."""
    with open(path, 'rb') as fh:
        raw = fh.read()
    text = raw.decode('utf-8')
    ms = list(STAMP_RE.finditer(text))
    if not ms:
        return None, None
    old = int(ms[-1].group(1))
    cur = text
    for _ in range(4):
        size = len(cur.encode('utf-8'))
        m = list(STAMP_RE.finditer(cur))[-1]
        cand = cur[:m.start(1)] + str(size) + cur[m.end(1):]
        if len(cand.encode('utf-8')) == size:
            cur = cand
            break
        cur = cand
    with open(path, 'wb') as fh:
        fh.write(cur.encode('utf-8'))
    return old, len(cur.encode('utf-8'))


def main(argv=None):
    ap = argparse.ArgumentParser(description='Rule 11 regeneration check (old → new).')
    ap.add_argument('files', nargs='+', help='file names, e.g. exercise-database.md')
    ap.add_argument('--date', required=True, help='review date YYYY-MM-DD (addenda / § check)')
    ap.add_argument('--old', default='/mnt/project', help='directory of the source (old) files')
    ap.add_argument('--new', default='.', help='directory of the regenerated (new) files')
    ap.add_argument('--old-suffix', default=None, help='old file = <stem><suffix><ext> in --new dir (e.g. _old)')
    ap.add_argument('--touched', action='append', default=[], help='NAME=k1,k2,… declared touched entries (repeatable)')
    ap.add_argument('--changelog', default=None, help='path of the NEW CHANGELOG.md (verifies retired footer lines)')
    ap.add_argument('--allow', action='append', default=[], help='NAME=old line numbers whose partial loss is a declared client-ordered removal')
    ap.add_argument('--archive', action='append', default=[], help='archive file(s); a line that left a live file passes as ARCHIVED when found verbatim in one of them (repeatable)')
    ap.add_argument('--restamp', action='store_true', help='rewrite the `bytes:` stamp of the given files in --new to their fixed-point size, then exit')
    ap.add_argument('--stamps', action='store_true', help='check only: size vs last `bytes:` stamp of the given files in --new (Rule 11(2)), then exit')
    args = ap.parse_args(argv)
    if args.stamps:
        bad = 0
        for name in args.files:
            path = os.path.join(args.new, os.path.basename(name))
            if not os.path.isfile(path):
                print('missing file: %s' % path, file=sys.stderr)
                return 2
            text, _, size, _ = read_lines(path)
            ms = STAMP_RE.findall(text)
            st = int(ms[-1]) if ms else None
            ok = st == size
            bad += 0 if ok else 1
            print('%s: size %d · stamp %s · %s' % (os.path.basename(name), size, st if st is not None else 'MISSING',
                                                    'OK' if ok else ('MISSING' if st is None else 'MISMATCH')))
        return 1 if bad else 0
    if args.restamp:
        for name in args.files:
            path = os.path.join(args.new, os.path.basename(name))
            if not os.path.isfile(path):
                print('missing file: %s' % path, file=sys.stderr)
                return 2
            old, new = restamp(path)
            print('%s: bytes: %s → %s' % (os.path.basename(name), old, new) if old is not None else '%s: no `bytes:` stamp found' % name)
        return 0
    allowed = {}
    for t in args.allow:
        if '=' not in t:
            print('bad --allow value: %s' % t, file=sys.stderr)
            return 2
        n, ks = t.split('=', 1)
        allowed[n] = set(int(k) for k in ks.split(',') if k.strip())

    decl = {}
    for t in args.touched:
        if '=' not in t:
            print('bad --touched value: %s' % t, file=sys.stderr)
            return 2
        n, ks = t.split('=', 1)
        decl[n] = [k.strip() for k in ks.split(',') if k.strip()]
    changelog_text = None
    if args.changelog:
        changelog_text = read_lines(args.changelog)[0]
    archive_text = None
    for ap_ in args.archive:
        if not os.path.isfile(ap_):
            print('missing archive file: %s' % ap_, file=sys.stderr)
            return 2
        archive_text = (archive_text or '') + '\n' + read_lines(ap_)[0]

    any_fail = False
    blocks = []
    for name in args.files:
        base = os.path.basename(name)
        new_path = os.path.join(args.new, base)
        if args.old_suffix:
            stem, ext = os.path.splitext(base)
            old_path = os.path.join(args.new, stem + args.old_suffix + ext)
        else:
            old_path = os.path.join(args.old, base)
        if not os.path.isfile(new_path):
            print('missing file: %s' % new_path, file=sys.stderr)
            return 2
        if not os.path.isfile(old_path):
            out, failed = check_new(base, new_path, args.date)
            any_fail = any_fail or failed
            blocks.append('\n'.join(out))
            continue
        cl_text = changelog_text
        if cl_text is None and base != 'CHANGELOG.md':
            cand = os.path.join(args.new, 'CHANGELOG.md')
            if os.path.isfile(cand):
                cl_text = read_lines(cand)[0]
        out, failed = check_pair(base, old_path, new_path, args.date, decl.get(base), cl_text,
                                 allowed.get(base, set()), archive_text)
        any_fail = any_fail or failed
        blocks.append('\n'.join(out))
    print('\n\n'.join(blocks))
    print('\nRESULT ALL: %s' % ('FAIL' if any_fail else 'PASS'))
    return 1 if any_fail else 0


if __name__ == '__main__':
    sys.exit(main())
