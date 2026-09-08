"""aichat_docs.py — ALL document processing for the aichat panel.

Keeps harmony-ai.py slim: PDF extract, plain-text read, sidecars,
page lookup, hybrid labeled-field pull. No NiceGUI. Heavy libs
(pymupdf/pymupdf4llm/pdfplumber) import lazily per call, so importing
this module is always cheap.
"""

import logging
import os
import re
import time
from pathlib import Path

from aichat_project_controller import find_doc_attach

TEXT_EXTS = ('.txt', '.md', '.log', '.csv', '.json')

# ============================================================
# RAW EXTRACTORS
# ============================================================

def extract_pymupdf(pdf_path):
    import pymupdf
    start = time.time()
    doc = pymupdf.open(pdf_path)
    full_text = []
    for page_num, page in enumerate(doc):
        full_text.append(f"--- PAGE {page_num + 1} ---\n{page.get_text('text')}")
    try:
        doc.close()
    except Exception:
        pass
    duration = time.time() - start
    return "\n".join(full_text), duration


def extract_pdfplumber(pdf_path):
    import pdfplumber
    start = time.time()
    full_text = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            full_text.append(f"--- PAGE {page_num + 1} ---\n{text}")
    duration = time.time() - start
    return "\n".join(full_text), duration


def extract_pdf_text(pdf_path):
    """Best-effort PDF -> text. Tries PyMuPDF, falls back to pdfplumber.

    Returns (text, backend_name). Never raises — returns ("", "none") on
    failure (e.g. scanned-image PDF with no text layer).
    """
    for _fn, _name in ((extract_pymupdf, 'pymupdf'),
                       (extract_pdfplumber, 'pdfplumber')):
        try:
            _text, _ = _fn(pdf_path)
            if _text and _text.strip():
                return _text, _name
        except Exception as _ex:
            logging.warning("PDF extract via %s failed for %s: %s",
                            _name, pdf_path, _ex)
    return "", "none"


def read_text_capped(path, max_chars=20000):
    """Plain-text read with a cap. Never raises — returns "" on failure."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as _f:
            return _f.read(max_chars + 1)[:max_chars]
    except Exception as _ex:
        logging.warning("text read failed for %s: %s", path, _ex)
        return ""


def extract_any(path, pages=None):
    """PDF (layout md when possible) or plain text. Returns (text, backend).

    pages: 0-based list for PDFs (e.g. [0, 4, 5]); None = whole doc.
    Never raises.
    """
    low = str(path or '').lower()
    if low.endswith('.pdf'):
        if pages is not None:
            md, backend = extract_pages(path, pages)
            if md.strip():
                return md, backend
        return extract_pdf_text(path)
    if low.endswith(TEXT_EXTS):
        return read_text_capped(path), "text"
    return "", "none"


def pdf_sidecar(path_str):
    """If path is a PDF, extract to a /tmp sidecar .txt and return that path.

    aichat `-f` PDF support depends on the build (needs its own reader);
    feeding pre-extracted text guarantees the model always sees the content.
    Non-PDF paths are returned unchanged. Never raises.
    """
    try:
        if not path_str or not str(path_str).lower().endswith('.pdf'):
            return path_str
        text, backend = extract_pdf_text(path_str)
        if not text.strip():
            logging.warning("PDF %s: no text layer (scanned images?) via %s",
                            path_str, backend)
            return path_str  # let aichat try natively as last resort
        os.makedirs('/tmp/aichat_docs', exist_ok=True)
        side = os.path.join(
            '/tmp/aichat_docs', Path(path_str).name + '.txt')
        with open(side, 'w', encoding='utf-8') as _f:
            _f.write(f"SOURCE FILE: {path_str} (extracted via {backend})\n\n{text}")
        logging.info("PDF %s -> %s via %s (%d chars)",
                     path_str, side, backend, len(text))
        return side
    except Exception as _ex:
        logging.warning("PDF sidecar failed for %s: %s", path_str, _ex)
        return path_str


def resolve_doc_file(arg, workdir=None, base_dir=None, selected_file=None):
    """Resolve a !pdf filename to a real .pdf/.txt/.md path. str or None."""
    cands = []
    if arg:
        a = arg.strip().strip('"').strip("'")
        p = Path(a)
        cands.append(p)
        for base in (workdir, base_dir):
            if base:
                cands.append(Path(base) / p.name)
                cands.append(Path(base) / a)
        if base_dir:
            try:
                for dp, dns, fns in os.walk(str(base_dir)):
                    depth = os.path.relpath(dp, str(base_dir)).count(os.sep)
                    if depth > 2 or '/venv_ui' in dp or '/.git' in dp:
                        dns[:] = []
                        continue
                    for f in fns:
                        if f.lower() == p.name.lower():
                            cands.append(Path(dp) / f)
            except Exception:
                pass
    elif selected_file:
        cands.append(Path(selected_file))
    for c in cands:
        try:
            if c.is_file() and str(c).lower().endswith(('.pdf',) + TEXT_EXTS):
                return str(c)
        except Exception:
            pass
    return None


def parse_pdf_args(arg):
    """Parse `/pdf [file] [--page N] [--first N|--top N] [--last N] [--insert|--extract]`.

    Simple switches only — no programming. Returns a dict:
    {'file','page','first','last','insert'}. If no file token is given, 'file'
    stays None so the caller falls back to the navigator-selected file.
    Never raises.
    """
    tokens = (arg or '').strip().split()
    out = {'file': None, 'page': None, 'first': None, 'last': None, 'insert': False}
    if tokens and tokens[0].lower().endswith(('.pdf', '.txt', '.md')):
        out['file'] = tokens.pop(0)
    i = 0
    while i < len(tokens):
        t = tokens[i].lower()
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if t in ('--page', '--pg', '-p') and nxt and nxt.isdigit():
            out['page'] = int(nxt)
            i += 2
            continue
        if t in ('--first', '--top', '-f') and nxt and nxt.isdigit():
            out['first'] = int(nxt)
            i += 2
            continue
        if t in ('--last', '-l') and nxt and nxt.isdigit():
            out['last'] = int(nxt)
            i += 2
            continue
        if t in ('--insert', '-i'):
            out['insert'] = True
        if t in ('--extract', '-e'):
            out['insert'] = False
        i += 1
    return out


def pdf_page_paragraphs(pdf_path, page_num):
    """Return (page_text, paragraphs, total_pages) for a 1-based page.

    paragraphs = non-empty blocks split on blank lines. Never raises."""
    import pymupdf
    try:
        doc = pymupdf.open(pdf_path)
        total = len(doc)
        if page_num < 1 or page_num > total:
            try:
                doc.close()
            except Exception:
                pass
            return "", [], total
        page_text = doc[page_num - 1].get_text('text')
        try:
            doc.close()
        except Exception:
            pass
        paras = [p.strip() for p in re.split(r'\n\s*\n', page_text) if p.strip()]
        return page_text, paras, total
    except Exception:
        return "", [], 0


# ============================================================
# DETERMINISTIC PAGE LOOKUP (no LLM)
# ============================================================

_PAGE_RE = re.compile(r'page\s+(\d+)', re.I)
_ITEM_RE = re.compile(r'item\s+(\d+)', re.I)
_NUMHEAD_RE = re.compile(r'^\s*(\d+)[\.\)]\s*(.*)$')
_LOOKUP_WORDS = ('list', 'contents', 'content', 'show', 'how many lines',
                 'count', 'number of lines', 'lines', 'lookup')
_REASON_WORDS = ('summar', 'explain', 'mean', 'compare', 'why',
                 'analyz', 'analyse')


def doc_page_lookup(prompt, base_dir=None, selected_file=None):
    """Answer pure page-lookup questions from local doc text, no LLM.

    Returns the reply text, or None to fall through to the model.
    """
    p = prompt or ''
    pl = p.lower()
    m = _PAGE_RE.search(pl)
    if not m or not any(w in pl for w in _LOOKUP_WORDS):
        return None
    if any(w in pl for w in _REASON_WORDS):
        return None
    pageno = int(m.group(1))
    target = None
    try:
        if selected_file and str(selected_file).lower().endswith(
                ('.pdf',) + TEXT_EXTS):
            target = selected_file
        elif base_dir:
            for doc in find_doc_attach(prompt, str(base_dir)):
                if str(doc).lower().endswith(('.pdf',) + TEXT_EXTS):
                    target = doc
                    break
    except Exception:
        pass
    if not target:
        return None
    name = Path(target).name
    try:
        if str(target).lower().endswith('.pdf'):
            import pymupdf
            _doc = pymupdf.open(target)
            if pageno < 1 or pageno > len(_doc):
                _n = len(_doc)
                try:
                    _doc.close()
                except Exception:
                    pass
                return (f"```text\n{name} has {_n} pages "
                        f"— page {pageno} out of range.\n```")
            page_text = _doc[pageno - 1].get_text('text')
            _doc.close()
        else:
            if pageno != 1:
                return (f"```text\n{name} is plain text (one block) "
                        f"— page {pageno} out of range.\n```")
            page_text = read_text_capped(target)
    except Exception:
        return None
    lines = page_text.splitlines()
    im = _ITEM_RE.search(pl)
    if im:
        want = im.group(1)
        start = None
        for i, ln in enumerate(lines):
            hm = _NUMHEAD_RE.match(ln)
            if hm and hm.group(1) == want:
                start = i
                break
        if start is None:
            return (f"```text\nPage {pageno} of {name}: "
                    f"no item {want} header found.\n```")
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if _NUMHEAD_RE.match(lines[j]):
                end = j
                break
        section = lines[start:end]
        nonempty = sum(1 for ln in section if ln.strip())
        body = '\n'.join(section).strip() or '(empty section)'
        return (f"**Page {pageno} — item {want} "
                f"({len(section)} lines, {nonempty} non-empty):**\n"
                f"```text\n{body}\n```")
    nonempty = sum(1 for ln in lines if ln.strip())
    body = page_text.strip() or '(empty page)'
    return (f"**Page {pageno} of {name} "
            f"({len(lines)} lines, {nonempty} non-empty):**\n"
            f"```text\n{body}\n```")


# ============================================================
# HYBRID: deterministic labeled-field pull + LLM semantic slice
# ============================================================

def extract_pages(pdf_path, pages):
    """Targeted pages -> markdown via pymupdf4llm. Returns (md, backend).

    Falls back to plain PyMuPDF text (no tables, no OCR). Never raises.
    """
    try:
        import pymupdf4llm as _m
        return _m.to_markdown(pdf_path, pages=list(pages)), "pymupdf4llm"
    except Exception:
        pass
    try:
        import pymupdf as _p
        doc = _p.open(pdf_path)
        out = []
        for i in pages:
            if 0 <= i < len(doc):
                out.append(f"--- PAGE {i + 1} ---\n{doc[i].get_text('text')}")
        try:
            doc.close()
        except Exception:
            pass
        text = "\n".join(out)
        return (text, "pymupdf-raw") if text.strip() else ("", "none")
    except Exception:
        return "", "none"


def _clean_cell(v):
    """Strip markdown/table noise from a pulled value."""
    v = re.sub(r'<br\s*/?>', ' ', v or '')
    v = re.sub(r'\*+', '', v)
    v = re.sub(r'\|+', ' ', v)
    return re.sub(r'\s+', ' ', v).strip(' :;-')


def pull_labeled_fields(md_text, labels):
    """Deterministic `Label: value` pull. Returns {label: value}.

    Handles markdown-table form layouts (`|Firm Name:<br>CAGE Code:|`).
    Never raises. Missing label -> "" (never hallucinated).
    """
    found = {}
    text = md_text or ''
    for label in labels:
        labpat = re.compile(re.escape(label) + r'\s*:', re.I)
        val = ""
        # first pass: value in the SAME <br>/cell segment as the label
        for line in text.splitlines():
            if not labpat.search(line):
                continue
            for seg in re.split(r'<br\s*/?>|\|', line):
                m = labpat.search(seg)
                if m:
                    cand = _clean_cell(seg[m.end():])
                    if cand:
                        val = cand
                        break
            if val:
                break
        if not val:
            # second pass: value on following line(s) with no labels on them
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if not labpat.search(line):
                    continue
                for nxt in lines[i + 1:i + 3]:
                    if ':' in nxt:
                        continue  # holds other labels/options, not our value
                    for seg in re.split(r'<br\s*/?>|\|', nxt):
                        if not seg.strip() or seg.strip().rstrip('*').rstrip().endswith(':'):
                            continue  # another label cell, not a value
                        cand = _clean_cell(seg)
                        if cand:
                            val = cand
                            break
                    if val:
                        break
                if val:
                    break
        found[label] = val
    return found


def semantic_slice(md_text, pulled):
    """Remainder for the LLM: text minus lines explained by pulls.

    Keeps token spend to the unstructured slice only. Never raises.
    """
    try:
        covered = set()
        for line in (md_text or '').splitlines():
            for v in (pulled or {}).values():
                if v and v in line:
                    covered.add(line)
                    break
        rest = [l for l in (md_text or '').splitlines() if l not in covered]
        return '\n'.join(rest).strip()
    except Exception:
        return md_text or ''


def hybrid_extract(pdf_path, pages, labels):
    """One call: (fields_dict, semantic_text, backend, elapsed_s). Never raises."""
    t0 = time.time()
    md, backend = extract_pages(pdf_path, pages)
    fields = pull_labeled_fields(md, labels)
    return fields, semantic_slice(md, fields), backend, time.time() - t0


# ============================================================
# COMMAND REGISTRY (drives the / and ! suggestion strip)
# ============================================================

# ============================================================
# COMMAND REGISTRY (drives the / and ! suggestion strip)
# ============================================================

COMMANDS = [
    {'cmd': '!pdf <file>', 'desc': 'Fast PDF extract → chat + .md, no LLM'},
    {'cmd': '/pdf <file>', 'desc': 'Same as !pdf'},
    {'cmd': '/pdf --page 1 --first 3', 'desc': 'Page 1, first 3 paragraphs (uses tree-selected file)'},
    {'cmd': '/pdf --page 1 --last 1 --insert', 'desc': 'Last para of page 1 → next prompt'},
    {'cmd': '/flow', 'desc': 'Open the project Flow Board'},
    {'cmd': '/clear', 'desc': 'Reset the conversation (forget previous subject)'},
    {'cmd': '/help', 'desc': 'Show all commands here'},
    {'cmd': '!ls -la', 'desc': 'List this folder (shell, instant)'},
    {'cmd': '!cat <file>', 'desc': 'Show a text file (shell, instant)'},
    {'cmd': '!grep <pattern>', 'desc': 'Search files (shell, instant)'},
]



def suggest_commands(prefix):
    """Registry matches for a '/' or '!' prefix. Never raises."""
    try:
        p = (prefix or '').lower()
        return [c for c in COMMANDS if c['cmd'].lower().startswith(p)]
    except Exception:
        return []
