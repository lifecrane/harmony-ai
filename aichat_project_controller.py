"""aichat_project_controller.py — PANEL'S COPY of project_controller.py.

Copied 2026-09-04 so the aichat panel never confuses with niceai's file.
RULE: nav/context fixes must land in BOTH files (they diverge otherwise).
References to "niceai" below are historical (origin), not imports — this
module is stdlib-only.

Origin: project_controller.py — Model-agnostic project management controller.

Why this exists
---------------
The panel's project flow (intake -> draft task order -> confirm -> execute) was
scattered inside niceai.py and depended on whichever model answered the chat
(lfm, nemotron, laguna, north-mini all behave slightly differently). That made
the flow inconsistent and fragile. This module owns that state machine so the
behaviour is DETERMINISTIC regardless of the underlying model.

The controller uses STRICT structured output (JSON) for parsing and DETERMINISTIC
task-order drafting (template + a narrow model fill), NOT freeform chat. It never
touches the UI; the panel calls into it and renders whatever it returns.

Design
------
* Pure functions + a small state object. No NiceGUI import here.
* Dependencies (call_model, run_with_failover, parse_task_order,
  default_test_cmd, default_model_id) are injected so it can be unit-tested in
  isolation and works without building the whole panel UI.
* State persists in memory (a dict) so a project's intake progress survives as
  long as the process is up. That's what "defined at boot, usable on reboot"
  means — no runtime re-wiring needed.
"""

from __future__ import annotations
import json
import os
import re
import time
import threading
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Task-order parsing + default test commands (moved out of niceai.py).
# These are pure leaf utilities — no UI, no model, no side effects.
# ---------------------------------------------------------------------------

_TASK_ORDER_META = ('workspace', 'files', 'file', 'execution', 'order', 'notes', 'note',
                    'overview', 'context', 'dependencies', 'dependency', 'summary', 'intro',
                    'general', 'resumption', 'background', 'purpose', 'goal',
                    'planning', 'done', 'blocked', 'review')

# Flow-board containers: `## <name>` sections that group tasks into tiles on the
# flow board. Not milestones — parse_task_order skips them via _TASK_ORDER_META.
FLOW_CONTAINERS = ('Goal', 'Dependencies', 'Planning', 'Execution', 'Done', 'Review',
                   'Blocked')


_CANON_CB_RE = re.compile(r'^\s*[-*]\s*\[[ xX]\]\s+')
# a bullet + bracket marker with 0 or 1 chars inside: "- []", "- [x]", "- [ ]"
_LOOSE_CB_RE = re.compile(r'^(\s*)[-*]\s*\[([ xX]?)\]\s*')

LAST_CHECKBOX_REPAIRS = []


def repair_malformed_checkboxes(text):
    """Fix checkbox markers that the parser would otherwise SWALLOW.

    THE BUG THIS CLOSES: `- []` (no space inside the brackets) does NOT match
    the canonical checkbox regex, so the line is treated as ordinary body text
    and gets appended to the PREVIOUS milestone. The whole spec disappears and
    the task is never run. That is exactly how neonrunner's "Add shooting
    capability" milestone (full spec, /test: and /browser: gates and all)
    silently vanished into the already-done "Lighten the background" task.

    Normalizes `- []` / `- [x]` / `-[]` to canonical `- [ ]` / `- [x]`.
    Returns (repaired_text, [(line_no, before, after), ...]).
    """
    global LAST_CHECKBOX_REPAIRS
    out, repairs = [], []
    for i, ln in enumerate((text or '').split('\n'), 1):
        if _CANON_CB_RE.match(ln):
            out.append(ln)
            continue
        m = _LOOSE_CB_RE.match(ln)
        if m:
            mark = 'x' if m.group(2).lower() == 'x' else ' '
            new = f"{m.group(1)}- [{mark}] {ln[m.end():]}"
            repairs.append((i, ln.strip()[:70], new.strip()[:70]))
            out.append(new)
        else:
            out.append(ln)
    LAST_CHECKBOX_REPAIRS = repairs
    return '\n'.join(out), repairs


def parse_task_order(text):
    """Splits task-order.md into ordered tasks (milestones).
    Checkbox style (- [ ] / - [x]) wins; otherwise '## ' sections (works even when
    the whole file is one long line); otherwise the whole text is a single task.
    Returns [{'label','text','done','kind','test_cmd','test_url','browser'}].
    A '/test: <cmd>' line attached to a task enables the runtime self-test gate.
    An optional '/url: http://...' line makes it a server probe.
    A '/browser: {json}' line makes it a headless-Chrome UI test.

    Malformed checkboxes (`- []`, no space) are repaired first — otherwise the
    milestone is silently swallowed by the previous task and never runs.
    """
    text, _repairs = repair_malformed_checkboxes(text)

    def _grab_fields(cur, s):
        s = s.strip()
        tm = re.match(r'^/test:\s*(.+)$', s)
        if tm:
            cur['test_cmd'] = tm.group(1).strip()
            return True
        um = re.match(r'^/url:\s*(\S+)$', s)
        if um:
            cur['test_url'] = um.group(1).strip()
            return True
        bm = re.match(r'^/browser:\s*(.+)$', s)
        if bm:
            cur['browser'] = bm.group(1).strip()
            return True
        return False

    text = (text or '').strip()
    if not text:
        return []
    if any(re.match(r'^\s*[-*]\s*\[[ xX]\]\s+', ln) for ln in text.split('\n')):
        result, cur = [], None
        for ln in text.split('\n'):
            m = re.match(r'^\s*[-*]\s*\[([ xX])\]\s+(.+?)\s*$', ln)
            if m:
                cur = {'label': m.group(2).strip(), 'text': m.group(2).strip(),
                       'done': m.group(1).lower() == 'x', 'kind': 'checkbox',
                       'test_cmd': None, 'test_url': None, 'browser': None}
                # Pending (brainstormed, unconfirmed) / obsolete (abandoned) markers.
                #  - a status word at the START of the label: #tmp (pending), #obsolete/#obs (abandoned)
                _label_l = cur['label'].lower()
                cur['pending'] = bool(re.match(r'^#tmp\b', _label_l))
                cur['obsolete'] = bool(re.match(r'^#(?:obsolete|obs|obsolet)\b', _label_l))
                result.append(cur)
                continue
            if cur is not None:
                s = ln.strip()
                if _grab_fields(cur, s):
                    continue
                # PRESERVE '## HEADING' body lines (FILE/WHAT/DO-NOT-TOUCH) — they are
                # the milestone's actual instruction, and the pipeline feeds task['text']
                # straight to the model. Skipping them (as before) silently gutted the
                # self-contained-spec format. Bare '#' headings are still skipped.
                if s and not s.startswith('/') and not (s.startswith('#') and not s.startswith('## ')):
                    cur['text'] = (cur['text'] + ' ' + s).strip()
        if result:
            return result
    tasks = []
    for part in re.split(r'(?=##\s+)', text):
        m = re.match(r'##\s+(.*)', part, re.DOTALL)
        if not m:
            continue
        rest = m.group(1)
        if '\n' in rest:
            header = rest.split('\n', 1)[0].strip()
        else:
            header = re.split(r'\s{2,}', rest, 1)[0].strip()
        first_word = re.sub(r'[^a-z0-9]', '', header.split(' ')[0].lower()) if header else ''
        if first_word in _TASK_ORDER_META:
            continue
        body = part.strip()
        test_cmd, test_url, browser = None, None, None
        tm = re.search(r'^/test:\s*(.+)$', body, re.MULTILINE)
        if tm:
            test_cmd = tm.group(1).strip()
        um = re.search(r'^/url:\s*(\S+)$', body, re.MULTILINE)
        if um:
            test_url = um.group(1).strip()
        bm = re.search(r'^\s*/browser:\s*(.+)$', body, re.MULTILINE)
        if bm:
            browser = bm.group(1).strip()
        tasks.append({'label': header, 'text': body, 'done': False, 'kind': 'section',
                      'test_cmd': test_cmd, 'test_url': test_url, 'browser': browser})
    if not tasks:
        tasks.append({'label': text[:80], 'text': text, 'done': False, 'kind': 'single',
                      'test_cmd': None, 'test_url': None, 'browser': None})
    return tasks


def parse_flow_containers(text):
    """Split a task-order.md into FLOW CONTAINERS for the flow board.

    A container is a `## <Name>` section whose name is a known FLOW_CONTAINERS
    word (Goal/Dependencies/Planning/Execution/Done/Review/Blocked — emoji or
    other prefixes are ignored). Tasks under each container are its checkbox /
    bullet lines. Text before the first container groups under 'Planning'.

    Returns a list of (container_name, [(done: bool, label: str), ...]).
    If there are no container sections, returns [] (caller falls back to a
    flat task chain). Never raises.
    """
    text = text or ''
    containers = []
    cur = None
    order = []

    def _core(s):
        # '## 🎯 Goal' / '## Goal' -> 'goal'
        return re.sub(r'[^a-z ]', '', (s or '').lower()).strip()

    for raw in text.split('\n'):
        m = re.match(r'^\s*##\s+(.+?)\s*$', raw)
        if m:
            title = m.group(1).strip()
            core = _core(title)
            name = next((c for c in FLOW_CONTAINERS if _core(c) == core), None)
            if name:
                cur = name
                if name not in order:
                    order.append(name)
                    containers.append([name, []])
                continue
        # Task line (checkbox or plain bullet) under the current container.
        tm = re.match(r'^\s*[-*]\s*(?:\[([ xX])\]\s*)?(.+?)\s*$', raw)
        if tm and cur is not None:
            mark = tm.group(1)
            done = (mark or '').lower() == 'x'
            label = (tm.group(2) or '').strip()
            if label and not label.startswith('/'):
                containers[-1][1].append((done, label))
    if not order:
        return []
    return containers


# ---------------------------------------------------------------------------
# Named test-command library (test_commands.md) — a cleaner way to write gates.
#
# Instead of inlining the whole `python3 -c "..."` command into every milestone,
# a project can define NAMED test gates once in `test_commands.md`:
#
#   ## structural-html
#   Use: any milestone that writes index.html
#   Command: python3 -c "import re;h=open('index.html',...).read(); assert ...; print('ok')"
#
#   ## boot-move
#   Use: game pages exposing window.__game
#   Browser: {"file":"index.html","boot_assert":"true","auto_move_smoke":true,"hook":"__game","timeout":45}
#
# Milestones then reference them by name:
#   /test: structural-html
#   /browser: boot-move
#
# `resolve_test_commands()` turns those names into the full command/browser dict
# at run time (the runner gets real strings/JSON, identical to inline gates).
# ---------------------------------------------------------------------------
_TEST_LIB_FILE = 'test_commands.md'


def parse_test_commands_lib(text):
    """Parse a test_commands.md file into {name: {'Command':..., 'Browser':..., 'Use':...}}.
    '## name' headers start a gate; indented 'Key: value' lines fill it. Returns {} if none."""
    lib = {}
    cur = None
    for ln in (text or '').splitlines():
        m = re.match(r'^\s*##\s+(.+?)\s*$', ln)
        if m:
            name = m.group(1).strip().lower().replace(' ', '-')
            cur = {'_name': m.group(1).strip()}
            lib[name] = cur
            continue
        if cur is None:
            continue
        km = re.match(r'^\s*([A-Za-z][A-Za-z0-9_]*)\s*:\s*(.+?)\s*$', ln)
        if km:
            key = km.group(1)
            val = km.group(2).strip()
            cur[key] = cur.get(key, '') + ('\n' if cur.get(key) else '') + val
    return lib


def load_test_commands_lib(proj_dir):
    """Read <proj_dir>/test_commands.md (if present) -> {name: {...}}. Never raises."""
    try:
        if not proj_dir:
            return {}
        path = os.path.join(proj_dir, _TEST_LIB_FILE)
        if not os.path.isfile(path):
            return {}
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return parse_test_commands_lib(f.read())
    except Exception:
        return {}


def resolve_test_commands(proj_dir, tasks):
    """Expand named '/test:' / '/browser:' references in parsed tasks against the
    project's test_commands.md library. A value that looks like a NAME (single word,
    no spaces, no 'python/bash/echo' prefix, no '[' for JSON) is looked up; if found
    its Command/Browser replaces the task field. Inline commands pass through
    unchanged. Returns the same list (mutated in place)."""
    lib = load_test_commands_lib(proj_dir)
    if not lib:
        return tasks
    for t in (tasks or []):
        tc = (t.get('test_cmd') or '').strip()
        if tc and not re.search(r'\s', tc) and not re.match(r'^(python|python3|bash|echo|sh|node|test)\b', tc) \
                and not tc.startswith('['):
            entry = lib.get(tc.lower())
            if entry and entry.get('Command'):
                t['test_cmd'] = entry['Command']
        br = (t.get('browser') or '').strip()
        if br and not br.startswith('{') and not re.search(r'\s', br):
            entry = lib.get(br.lower())
            if entry and entry.get('Browser'):
                t['browser'] = entry['Browser']
    return tasks


def promote_pending_milestones(task_order_text):
    """Turn every '#tmp ...' (pending) line into a permanent '- [ ] ...' milestone.
    '#obsolete' lines are left as-is (ignored by the runner). Returns the new text."""
    out = []
    for ln in (task_order_text or '').split('\n'):
        m = re.match(r'^(\s*[-*]\s*\[[ xX]\]\s+)#(?:tmp)\s+(.*)$', ln)
        if m:
            out.append(f"{m.group(1)}{m.group(2)}")
        else:
            out.append(ln)
    return '\n'.join(out)


def pending_milestones(task_order_text):
    """The brainstormed-but-unconfirmed '#tmp' ideas (not yet permanent milestones)."""
    return [m.group(1).strip()
            for m in re.finditer(r'^\s*[-*]\s*\[[ xX]\]\s+#tmp\s+(.+)$', (task_order_text or ''), re.MULTILINE)]


def obsolete_milestones(task_order_text):
    """The '#obsolete' (abandoned) ideas — kept on file for reference, ignored by the runner."""
    return [m.group(1).strip()
            for m in re.finditer(r'^\s*[-*]\s*\[[ xX]\]\s+#(?:obsolete|obs|obsolet)\s+(.+)$',
                                 (task_order_text or ''), re.IGNORECASE | re.MULTILINE)]


def delete_milestone(task_order_text, match_text):
    """HARD-delete a task from a task-order (permanent removal, not #obsolete). Use
    when project goals/requirements change and a task is no longer wanted at all.

    Handles BOTH checkbox style ('- [ ] label' + indented body + '/test:' + '/browser:')
    and '## section' style (section header + body + directives). The match is a
    case-insensitive substring of the task's label/first line. Returns the new text
    (unchanged if nothing matched)."""
    text = task_order_text or ''
    lines = text.split('\n')
    m = re.match(r'^\s*(\[(section|STYLE)\] )?- ?(\[ |\[x|\[X\]|#(tmp|obsolete)\s+)(.*)$',
                 (lines[0] if lines else ''))
    # checkbox lines start with space `- [ ]` (or `- [x]` or `- [ ] #tmp` / `#obsolete`)
    out = []
    i = 0
    matched = False
    needle = (match_text or '').strip().lower()
    if not needle:
        return text
    while i < len(lines):
        ln = lines[i]
        cm = re.match(r'^\s*[-*]\s*\[[ xX]\]\s+(?:#(?:tmp|obsolete)\s+)?(.+)$', ln)
        sm = re.match(r'^\s*##\s+(.+)$', ln)
        if cm or sm:
            label = (cm.group(1) if cm else sm.group(1)).strip()
            if needle in label.lower():
                # skip this task's label line + its indented continuation body + directives
                matched = True
                j = i + 1
                while j < len(lines):
                    nxt = lines[j]
                    # a checkbox task's continuation lines are indented (space-prefixed);
                    # stop at the next non-indented task/header/blank-that-starts-a-new-task
                    if re.match(r'^\s*[-*]\s*\[[ xX]\]\s+', nxt) or \
                       re.match(r'^\s*##\s+', nxt):
                        break
                    if nxt.strip() and not nxt.startswith((' ', '\t')):
                        # unindented non-task line that's part of a section body — keep
                        break
                    j += 1
                i = j
                continue
        out.append(ln)
        i += 1
    if not matched:
        return text
    return '\n'.join(out).rstrip() + ('\n' if out else '')


def edit_milestone(task_order_text, match_text, new_label=None, new_body=None):
    """Edit a task in a task-order in place (label and/or body). Returns the new text.
    Matches a task by case-insensitive substring; replaces its label with new_label
    (if given) and its body with new_body (if given). Works on checkbox and '## section'
    styles. For section style the body is the paragraph after the '##' header."""
    text = task_order_text or ''
    lines = text.split('\n')
    needle = (match_text or '').strip().lower()
    if not needle:
        return text
    out = []
    i = 0
    changed = False
    while i < len(lines):
        ln = lines[i]
        cm = re.match(r'^\s*([-*]\s*\[[ xX]\]\s+)(?:(#(?:tmp|obsolete)\s+))?(.+)$', ln)
        sm = re.match(r'^\s*##\s+(.+)$', ln)
        if cm or sm:
            if cm:
                label = cm.group(3).strip()
            else:
                label = sm.group(1).strip()
            if needle in label.lower():
                if cm:
                    prefix = cm.group(1) + (cm.group(2) or '')
                    new_line = f"{prefix}{new_label or label}"
                    out.append(new_line)
                    # rewrite the following indented body block (until next task) if new_body
                    j = i + 1
                    if new_body is not None:
                        # remove old continuation body, replace with new indented body
                        k = j
                        while k < len(lines) and not re.match(r'^\s*[-*]\s*\[[ xX]\]\s+', lines[k]) \
                              and not re.match(r'^\s*##\s+', lines[k]):
                            k += 1
                        # drop tested directives? we preserve /test: etc unless new_body given wholesale
                        if new_body:
                            out.append("\n  " + "\n  ".join(new_body.split('\n')) if new_body else "")
                        i = k
                    else:
                        i = j
                    changed = True
                    continue
                else:
                    # section style: replace the header line; keep body unless new_body given
                    out.append(f"## {new_label or label}")
                    if new_body is not None:
                        j = i + 1
                        while j < len(lines) and not re.match(r'^##\s+', lines[j]) and not re.match(r'^\s*[-*]\s*\[[ xX]\]\s+', lines[j]):
                            j += 1
                        out.append(new_body)
                        i = j
                    else:
                        i += 1
                    changed = True
                    continue
        out.append(ln)
        i += 1
    if not changed:
        return text
    return '\n'.join(out)


def append_tmp_milestone(task_order_text, feature_text):
    """Append a brainstormed-but-unconfirmed idea as a '#tmp' pending line (so the
    team does NOT run it until it's promoted). Returns the updated task-order text."""
    feature_text = (feature_text or '').strip()
    if not feature_text:
        return task_order_text
    # auto dedup: only exact normalized duplicate → silent ignore; near-duplicate handled by niceai ask
    try:
        existing = pending_milestones(task_order_text) or []
        existing_norm = {re.sub(r'\W+', ' ', p).strip().lower() for p in existing}
        if re.sub(r'\W+', ' ', feature_text).strip().lower() in existing_norm:
            return task_order_text
    except Exception:
        pass
    to = (task_order_text or '').rstrip('\n')
    return to + f"\n- [ ] #tmp {feature_text[:300]}"


def apply_forget(task_order_text, idea_text):
    """Mark a #tmp idea as #obsolete (abandoned, ignored but recoverable).
    Returns the updated task-order text (caller saves)."""
    idea = (idea_text or '').strip().lower()
    out = []
    for ln in (task_order_text or '').split('\n'):
        m = re.match(r'^\s*[-*]\s*\[[ xX]\]\s+#tmp\s+(.+)$', ln)
        if m and (not idea or idea in m.group(1).strip().lower()):
            out.append(re.sub(r'#tmp', '#obsolete', ln, count=1))
        else:
            out.append(ln)
    return '\n'.join(out)


def collect_state(task_order_text):
    """Describe the current pending/obsolete/confirmed ideas for the UI/conversation."""
    pending = pending_milestones(task_order_text)
    obsolete = obsolete_milestones(task_order_text)
    confirmed = [t['label'] for t in parse_task_order(task_order_text)
                 if not t.get('pending') and not t.get('obsolete')]
    return {'pending': pending, 'obsolete': obsolete, 'confirmed': confirmed}


def default_test_cmd(task_text):
    """Generate a sensible default /test: command for a milestone based on the
    file(s) it mentions. Returns None if nothing testable is found.

    Task-agnostic foundation: code AND document tasks get a real gate. A
    document (brochure / proposal / letter) is verified by structure + any
    image/logo it references — never by py_compile."""
    if not task_text:
        return None
    low = task_text.lower()
    m = re.search(r'([a-z0-9_.\-]+\.(?:py|html?|js|ts|sh|md|rst|txt))', low)
    if not m:
        return None
    fname = m.group(1)
    if fname.endswith('.py'):
        return f"python3 -m py_compile {fname}"
    if fname.endswith('.html'):
        # Require the file to be granularly section-tagged (=== NAME === markers) so the
        # pipeline's surgical-edit engine can splice one section instead of regenerating the
        # whole file (which re-introduces bugs). Without this gate, the coder writes a few
        # broad markers (or none) and the loop falls back to full-file rewrites -> re-bug loop.
        return (f"python3 -c \"import re;h=open('{fname}',encoding='utf-8',errors='replace').read(); "
                f"assert '</html>' in h and len(h) > 100; "
                f"sec=re.findall(r'===\\s*[A-Z0-9_ ]+?\\s*===', h); "
                f"assert len(sec) >= 6, f'only {{len(sec)}} section markers, need >=6 for surgical edits'; "
                f"print('{fname} structural check OK', len(h), '| sections', len(sec))\"")
    if fname.endswith(('.md', '.rst', '.txt')):
        # DOCUMENT task: verify the doc is non-trivial and that any image/logo the
        # task asks for is actually referenced and present on disk. This is the
        # foundation for document workers (brochure / proposal / letter / logo).
        img_checks = ""
        for name in re.findall(r'([\w.\-]+\.(?:png|jpe?g|gif|svg|webp))', low):
            safe = name.replace("'", "")
            img_checks += (f"assert os.path.exists('{safe}') or os.path.exists("
                           f"os.path.join(os.path.dirname('{fname}'), '{safe}')), "
                           f"'referenced image {safe} missing'; "
                           f"assert '{safe}' in h, 'task mentions {safe} but doc does not reference it'; ")
        return (f"python3 -c \"import os,re;h=open('{fname}',encoding='utf-8',errors='replace').read(); "
                f"assert len(h) > 200, 'document too small'; {img_checks}"
                f"print('{fname} document check OK', len(h), 'chars')\"")
    return f"test -f {fname} && echo '{fname} exists'"


# ---------------------------------------------------------------------------
# Deterministic classification (NO model involved)
# ---------------------------------------------------------------------------

# Artifact / project-kind vocabulary we know how to build.
_ARTIFACT = r'(game(?:s)?|web ?app(?:s)?|website(?:s)?|web ?site(?:s)?|site(?:s)?|app(?:s)?|project(?:s)?|tool(?:s)?|page(?:s)?|dashboard(?:s)?|bot(?:s)?|landing page(?:s)?|store(?:s)?|connector(?:s)?|module(?:s)?|script(?:s)?|service(?:s)?|integration(?:s)?)'
# Project TYPE vocabulary presented to the user.
PROJECT_TYPE_OPTIONS = [
    "an app or website people use over the internet, or on your local LAN",
    "a classic website (info/brochure/landing page)",
    "a phone app (Android/iOS)",
    "a hardware project that needs software / automation",
]


def infer_project_type(text: str) -> Optional[str]:
    """Auto-detect the project type from the request text when it's obvious.
    Returns one of PROJECT_TYPE_OPTIONS or None (keep asking)."""
    rl = (text or '').lower()
    if any(k in rl for k in ('web app', 'webapp', 'online app', 'internet app',
                             'browser game', 'browser based game', 'retro game',
                             'html game', 'javascript game', 'js game', 'game',
                             'canvas', 'html css', 'html css js', 'css js',
                             'one page', 'single page')):
        return PROJECT_TYPE_OPTIONS[0]
    if any(k in rl for k in ('classic website', 'website', 'web site', 'landing page',
                             'brochure', 'info site')):
        return PROJECT_TYPE_OPTIONS[1]
    if any(k in rl for k in ('phone app', 'iphone', 'android', 'mobile')):
        return PROJECT_TYPE_OPTIONS[2]
    if any(k in rl for k in ('hardware', 'arduino', 'raspberry', 'automation', 'iot', 'device')):
        return PROJECT_TYPE_OPTIONS[3]
    return None


def _has_project_ref(s: str) -> bool:
    """True when the (lowercased, no-punct) text references an EXISTING project
    folder/scoped noun — e.g. a possessive (my/this/our project), 'the project folder',
    or an action on a scoped project ('to my project', 'for the project', 'in this app').
    Explicit NAMING constructs ("the name of the project/app is X", "is called/named X",
    "a game called X") describe a NEW thing and must NOT count as a reference —
    that let a new-project request look like an edit."""
    # Naming constructs → definitely NOT a reference to an existing folder.
    if re.search(r'\b(name of|named|called|by the name of|is called|is named)\b\s+(the\s+)?(project|app|game|site|website|web ?app|folder|application)\b', s) or \
       re.search(r'\b(the|this|my|our)\s+(project|app|game|site|folder)\b\s+is (?:called|named|going to be called)\b', s):
        return False
    return bool(re.search(r'\b(my|this|our)\s+(project|folder)\b', s)) or \
        bool(re.search(r'\bproject folder\b', s)) or \
        bool(re.search(r'\b(the|my|this|our)\s+(project|folder|app|website|site|bot|script|tool)\b\s+(?:needs|should|could|has|does|is|about|for)', s)) or \
        bool(re.search(r'\b(for|in|on|into|to)\s+(my|this)\s+\w*\s*(project|folder|app|dashboard|bot|site|website|application)\b', s)) or \
        bool(re.search(r'\b(project|app|website|bot|script|tool)\b for \b(my|the|our)\b', s))


def classify_build_request(text: str, chat_mode: bool = True) -> dict:
    """Determine whether `text` is a NEW-project build request (and if so, what).
    Returns {'is_build': bool, 'kind': 'new_project'|'add_feature'|None,
             'project_name': str|None, 'artifact': str|None}.
    Pure regex — deterministic, model-free, unit-testable."""
    t = (text or '').strip()
    l = t.lower()
    nl = re.sub(r'[^a-z0-9 ]', ' ', l)

    is_question = bool(re.search(r'\b(what is|how do|how does|why|who|tell me about|explain)\b', nl))
    is_new_verb = bool(re.search(r'\b(create|build|make|start|set up|start building|new project)\b', nl))
    has_artifact = bool(re.search(r'\b' + _ARTIFACT + r'\b', nl))

    # 1) Explicit: "build/make/create a (new) <artifact> (called/named) <name>"
    m = re.search(r'\b(create|build|make|start|set up|start building)\b[^.]{0,40}?'
                  r'\b(a|an|the|new)?\s*(' + _ARTIFACT + r')\b[^.]{0,40}?\b(called|named|for|to be called|with the name)\b', nl)
    if m:
        name = _extract_name(t)
        return {'is_build': True, 'kind': 'new_project', 'project_name': name, 'artifact': m.group(3)}

    # 1b) "build me a <artifact>..." / "make us a <artifact>..." — a NEW build with a
    #     pronoun, no name and no project reference. NOT an edit of an existing project.
    mb = re.search(r'\b(create|build|make|start|set up)\b\s+(?:me|us|for me|for us)\s+'
                   r'(?:a|an|the)\s+(' + _ARTIFACT + r')\b', nl)
    if mb and not _has_project_ref(nl):
        return {'is_build': True, 'kind': 'new_project', 'project_name': None, 'artifact': mb.group(1)}

    # 1c) "...(decided) to do/make a <artifact> (called/named) <Name>" — real spoken phrasing,
    #     e.g. "I decided this time to do a game that is well-known called Tetris."
    msc = re.search(r'\b(do|make|build|create|start|write|code)\b\s+(?:a|an|the)\s+' + _ARTIFACT +
                    r'\b[^.]{0,50}?\b(called|named)\b\s+([a-z][a-z0-9_\- ]{1,30})', nl)
    if msc and not is_question:
        artifact = msc.group(2)
        name = (msc.group(4) or '').strip().split()[0]
        return {'is_build': True, 'kind': 'new_project', 'project_name': name, 'artifact': artifact}

    # 1d) Generic but clear NEW build, no name yet: "build/make a new game/webapp/website...".
    #     No existing-project reference and not a question => a brand-new thing to build.
    #     (name=None deliberately — the planning wizard will ask for the name + type.)
    mg = re.search(r'\b(create|creat(?:ing|es)?|build(?:ing|s)?|make|making|start|developing?|start building|set up|do|doing)\b'
                   r'[^.]{0,25}?\b(?:a|an|the|new|another)?\s*(' + _ARTIFACT + r')\b', nl)
    if mg and not _has_project_ref(nl) and not is_question:
        return {'is_build': True, 'kind': 'new_project', 'project_name': None, 'artifact': mg.group(2)}

    # 1e) "make/build/create ... a NEW <artifact>" — same NEW-build intent as 1d, but
    #     tolerant of filler/typos between the verb and the artifact ("make a new pacman
    #     game", "build ane wproject a new pacman game"). Rule 1d's 25-char window is too
    #     tight once an interleaved phrase (e.g. "a new project") pushes the artifact out.
    mg2 = re.search(r'\b(create|creat(?:ing|es)?|build(?:ing|s)?|make|making|start|developing?|'
                    r'start building|set up|do|doing)\b[^.]{0,60}?'
                    r'\bnew\s+(?:[a-z0-9_\- ]{0,20}?\s)?(' + _ARTIFACT + r')\b', nl)
    if mg2 and not _has_project_ref(nl) and not is_question:
        return {'is_build': True, 'kind': 'new_project', 'project_name': None, 'artifact': mg2.group(2)}

    # 2) Folder: "create a folder called <name>"
    m = re.search(r'\b(folder|project)\b[^.]{0,25}?\b(called|named)\b\s+([a-z][a-z0-9_\- ]{1,24})', nl)
    if m:
        return {'is_build': True, 'kind': 'new_project', 'project_name': m.group(3).strip(), 'artifact': 'project'}

    # 3) Descriptive: "<Name> is a <artifact> ... (built/made/using) <tech>" — NOT a question.
    desc = bool(re.search(
        r"\b[a-z][a-z0-9_\- ]{1,24}\b[^.]{0,20}?\bis (?:a|an|the)\s+(?:new\s+)?"
        r"(game|web ?app|website|web ?site|site|app|project|tool|page|dashboard|bot|landing page|store)\b"
        r"[^.]{0,60}?(?:\b(built|made|written|coded|html|css|javascript|js)\b|\b(about|with|using)\b)", nl))
    if desc and not is_question:
        name = _extract_name(t)
        return {'is_build': True, 'kind': 'new_project', 'project_name': name, 'artifact': None}

    # 4) Add-feature-to-existing: "for my <project> add/build <thing>" + an artifact.
    #    A project REFERENCE is required — a bare "build me a dashboard" with no
    #    "my/this/the project" is a NEW project, not an edit to an existing one.
    is_edit_verb = bool(re.search(
        r'\b(add|implement|build|create|make|code|write|develop|fix|improve|update|'
        r'set up|give it|insert|integrate|wire)\b', nl))
    has_project_ref = _has_project_ref(nl)
    has_edit_target = bool(re.search(
        r'\b(login|feature|page|button|module|screen|view|tool|ability|support for|'
        r'option|setting|integrate|api|database|db|panel|section|counter|table)\b', nl))
    if is_edit_verb and has_project_ref:
        return {'is_build': True, 'kind': 'add_feature', 'project_name': None, 'artifact': None}

    return {'is_build': False, 'kind': None, 'project_name': None, 'artifact': None}


def _extract_name(text: str) -> Optional[str]:
    """Best-effort name extraction: the token after 'call/called/named/call it/name it'."""
    l = (text or '').strip().lower()
    for pat in (r'\bcall(?:ed)? (?:it |the (?:project|folder) )?', r'\bname(?:d)? (?:it |the (?:project|folder) )?'):
        m = re.search(pat + r'([a-z0-9_\-]{1,40})', l)
        if m:
            return m.group(1).lower()
    return None


def next_intake_question(state: dict) -> Optional[str]:
    """Return the next plain-English question to ask the user for the given intake
    state. Deterministic (no model). Returns None when intake is complete."""
    if not state.get('project_type'):
        opts = "\n".join(f"- **{i}.** {t}" for i, t in enumerate(PROJECT_TYPE_OPTIONS, 1))
        return (f"It's a **{state.get('artifact') or 'project'}** — what KIND of project?\n{opts}")
    if not state.get('name'):
        return "What would you like to call the project folder?"
    if not state.get('details'):
        return "Tell me a little about it — key features, look/feel, tech, pages?"
    return None


# ---------------------------------------------------------------------------
# Deterministic task-order drafting (template + narrow model fill)
# ---------------------------------------------------------------------------

def _sanitize_milestone(s: str) -> str:
    s = (s or '').strip()
    s = re.sub(r'^[-*]\s*\[[ xX]\]\s+', '', s)          # strip checkbox prefix if any
    s = re.sub(r'^[-*]\s+', '', s)
    s = re.sub(r'\s{2,}', ' ', s)
    s = re.sub(r'`+', '', s)
    return s[:200].strip()


def fallback_milestones(artifact: Optional[str]) -> list[str]:
    """Deterministic milestones for a brand-new build when no model is available."""
    a = (artifact or 'project')
    if 'game' in a:
        return [
            f"Build index.html — a complete playable {a}: game loop, player movement, "
            "obstacles, collision, score, game-over/restart. One self-contained page (HTML+CSS+JS).",
            "Add keyboard controls + a jump animation for the player character.",
            "Add obstacle generation and collision detection (game over on hit).",
            "Add a running score + a restart button after game over.",
        ]
    return [
        f"Scaffold the project (index.html + style + JS) for the {a}.",
        "Implement the core page logic and layout.",
        "Test in the browser and fix any issues.",
    ]


def draft_task_order(request: str, artifact: Optional[str] = None,
                     parse_func: Optional[Callable] = None,
                     default_test: Optional[Callable] = None,
                     pick_milestones: Optional[Callable] = None) -> str:
    """Draft a checkbox-style task-order.md (with /test: gates) from a request.
    Uses `pick_milestones(request) -> [str]` to let the caller supply model-based
    milestone drafting; falls back to deterministic `fallback_milestones`."""
    if not (request or '').strip():
        return ''
    if pick_milestones:
        try:
            milestones = pick_milestones(request)
        except Exception:
            milestones = []
    else:
        milestones = []
    if not milestones:
        milestones = fallback_milestones(artifact)
    out = []
    for ms in milestones:
        line = _sanitize_milestone(ms)
        if not line:
            continue
        out.append(f"- [ ] {line}")
        tc = default_test(line) if default_test else None
        if tc:
            out.append(f"/test: {tc}")
    if not out:
        out.append(f"- [ ] {request.strip()[:160]}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# The controller state machine — intake -> confirm -> execute
# ---------------------------------------------------------------------------

class ProjectController:
    """Holds per-project intake state and advances it one step per call.
    load()/save() make progress survive UI restarts as long as the process is up."""

    def __init__(self, call_model: Optional[Callable] = None,
                 default_model: Optional[Callable] = None,
                 parse_task_order: Optional[Callable] = None,
                 default_test_cmd: Optional[Callable] = None):
        self._call_model = call_model
        self._default_model = default_model or (lambda: 'lfm2.5-2.6b:latest')
        self._parse_task_order = parse_task_order
        self._default_test_cmd = default_test_cmd
        self._states: dict[str, dict] = {}
        self._lock = threading.Lock()

    # -- persistence (in-memory; survives process uptime) -------------------
    def load(self, project: str) -> dict:
        with self._lock:
            return dict(self._states.get(project, {}))

    def save(self, project: str, state: dict) -> None:
        with self._lock:
            self._states[project] = dict(state)

    def clear(self, project: str) -> None:
        with self._lock:
            self._states.pop(project, None)

    # -- intake -------------------------------------------------------------
    def start_intake(self, project: str, raw_request: str, artifact: Optional[str] = None) -> dict:
        st = {
            'raw_request': raw_request,
            'artifact': artifact,
            'project_type': None,
            'name': None,
            'details': '',
            'step': 'type',       # type -> name -> details -> confirm
            'created_at': time.time(),
        }
        self.save(project, st)
        return st

    def advance(self, project: str, user_reply: str) -> dict:
        """Feed the user's reply into the intake. Returns an updated state dict with
        'question' (what to ask next) and 'phase' ('intake'|'confirm'|'done')."""
        st = self.load(project)
        reply = (user_reply or '').strip()
        rl = reply.lower()

        cancel = any(w in rl for w in ('cancel', 'never mind', 'nevermind', 'stop', 'nothing', 'cancelar'))
        if cancel:
            self.clear(project)
            return {'phase': 'cancelled'}

        # --- type selection ---
        if not st.get('project_type'):
            st['project_type'] = self._match_type(reply)
            if not st['project_type'] and any(w in rl for w in ('not sure', 'unsure', "don't know", 'dont know')):
                st['project_type'] = 'unsure'
            self.save(project, st)
            q = next_intake_question(st)
            return {'phase': 'intake', 'project': project, 'question': q, 'state': st}

        # --- name ---
        if not st.get('name'):
            st['name'] = _extract_name(reply) or _tidy_name(reply)
            if not st['name']:
                return {'phase': 'intake', 'question': "A short name for the folder, please? (e.g. `neonrunner`)"}
            self.save(project, st)
            q = next_intake_question(st)
            return {'phase': 'intake', 'project': project, 'question': q, 'state': st}

        # --- details ---
        if not st.get('details'):
            st['details'] = reply
            self.save(project, st)
            return {
                'phase': 'confirm',
                'project': project,
                'draft': draft_task_order(
                    self._full_request(st),
                    artifact=st.get('artifact'),
                    parse_func=self._parse_task_order,
                    default_test=self._default_test_cmd,
                    pick_milestones=(lambda r: [st['details']]) if not self._default_test_cmd else None,
                ),
                'state': st,
            }
        # details already set (user refined) -> they're confirming or refining
        return {'phase': 'confirm', 'project': project, 'state': st}

    def _match_type(self, reply: str) -> Optional[str]:
        rl = reply.lower()
        for i, t in enumerate(PROJECT_TYPE_OPTIONS, 1):
            if re.search(r'\b%d\b' % i, rl):
                return t
        if any(k in rl for k in ('web app', 'webapp', 'online app', 'internet app',
                                 'browser game', 'browser based game', 'retro game', 'html game', 'javascript game', 'game')):
            return PROJECT_TYPE_OPTIONS[0]
        if any(k in rl for k in ('classic website', 'website', 'web site', 'landing page', 'brochure', 'site')):
            return PROJECT_TYPE_OPTIONS[1]
        if any(k in rl for k in ('phone app', 'iphone', 'android', 'mobile')):
            return PROJECT_TYPE_OPTIONS[2]
        if any(k in rl for k in ('hardware', 'arduino', 'raspberry', 'automation', 'iot', 'device')):
            return PROJECT_TYPE_OPTIONS[3]
        return None

    def _full_request(self, st: dict) -> str:
        parts = [st.get('raw_request') or '']
        if st.get('project_type'):
            parts.append(f"project type: {st['project_type']}")
        if st.get('name'):
            parts.append(f"project name: {st['name']}")
        if st.get('details'):
            parts.append(f"details: {st['details']}")
        return ". ".join(p for p in parts if p and p.strip())


def _tidy_name(t: str) -> Optional[str]:
    """Lowercase + keep only safe folder chars, first token."""
    s = re.sub(r'[^a-zA-Z0-9_-]', '', t or '').strip()
    return s[:50].lower() or None


# ---------------------------------------------------------------------------
# Project TEMPLATES (rarely used) — moved out of niceai.py.
# These depend on filesystem paths; we accept them as params so the module stays
# self-contained and unit-testable without the panel globals.
# ---------------------------------------------------------------------------

# Starter task.md text per template type (used when a brand-new type is created).
TEMPLATE_STARTERS = {
    'game': """# {PROJECT_NAME} — Game

**Created:** {TODAY}
**Type:** game
**Summary:** {SUMMARY}

## Game Spec
- Genre / style: (arcade, puzzle, platformer, ...)
- Controls: (keyboard / mouse / touch)
- Score + lives / levels
- Self-contained (no external libs) unless stated

## Tasks
- [ ] Define the game loop
- [ ] Build canvas renderer + entities
- [ ] Input handling (move / shoot / jump)
- [ ] Win / lose / game-over states
- [ ] Test in the browser

## Contacts
- (add point of contact)
""",
    'dashboard': """# {PROJECT_NAME} — Dashboard

**Created:** {TODAY}
**Type:** dashboard
**Summary:** {SUMMARY}

## Tasks
- [ ] Define the metrics / data sources
- [ ] Layout: KPI cards + charts + a table
- [ ] Data refresh / API wiring
- [ ] Empty & error states
- [ ] Test in the browser
""",
    'general': """# {PROJECT_NAME}

**Created:** {TODAY}
**Type:** general
**Summary:** {SUMMARY}

## Tasks
- [ ] (add the first task here)
""",
}


def list_project_templates(templates_root):
    """Returns {type_name: [file names]} from the templates folder."""
    result = {}
    if not os.path.isdir(templates_root):
        return result
    for item in sorted(os.listdir(templates_root)):
        item_path = os.path.join(templates_root, item)
        if os.path.isdir(item_path) and not item.startswith('.'):
            result[item] = sorted(f for f in os.listdir(item_path)
                                  if os.path.isfile(os.path.join(item_path, f)))
    return result


def ensure_template_starter(template_type, templates_root, project_name='PROJECT', summary=''):
    """Ensures a template type has a task.md starter file. Returns the file path or None."""
    t = re.sub(r'[^a-zA-Z0-9_-]', '', str(template_type or '')) or 'general'
    tmpl_dir = os.path.join(templates_root, t)
    os.makedirs(tmpl_dir, exist_ok=True)
    task_path = os.path.join(tmpl_dir, 'task.md')
    if os.path.exists(task_path):
        return task_path
    starter = TEMPLATE_STARTERS.get(t) or TEMPLATE_STARTERS['game']
    content = (starter
               .replace('{PROJECT_NAME}', project_name)
               .replace('{TODAY}', time.strftime('%Y-%m-%d'))
               .replace('{SUMMARY}', (summary or '')[:300]))
    try:
        with open(task_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return task_path
    except Exception:
        return None


def create_project_from_template(project_dir, project_type, templates_root,
                                 biz_card_file=None, biz_logo_file=None):
    """Copies starter files from _templates/<type>/ into a new project folder."""
    tmpl_dir = os.path.join(templates_root, project_type or 'general')
    if not os.path.isdir(tmpl_dir):
        tmpl_dir = os.path.join(templates_root, 'general')
    os.makedirs(project_dir, exist_ok=True)
    copied = []
    for f in os.listdir(tmpl_dir):
        src = os.path.join(tmpl_dir, f)
        if os.path.isfile(src) and not f.startswith('.'):
            try:
                import shutil
                dst = os.path.join(project_dir, f)
                shutil.copy2(src, dst)
                copied.append(f)
            except Exception:
                pass
    if (project_type or '').startswith('proposal'):
        try:
            import shutil
            if biz_card_file and os.path.isfile(biz_card_file):
                shutil.copy2(biz_card_file, os.path.join(project_dir, 'contact_card.md'))
                copied.append('contact_card.md')
            if biz_logo_file and os.path.isfile(biz_logo_file):
                os.makedirs(os.path.join(project_dir, 'img'), exist_ok=True)
                shutil.copy2(biz_logo_file, os.path.join(project_dir, 'img', 'logo.png'))
                copied.append('img/logo.png')
        except Exception:
            pass
    return copied


def save_project_as_template(project_name, workspace_root, templates_root, project_type=None):
    """Copies a project's structure into _templates/<type>/ as a reusable template."""
    src_dir = os.path.join(workspace_root, project_name)
    if not os.path.isdir(src_dir):
        return None, 'project not found'
    t = project_type or 'general'
    t = re.sub(r'[^a-zA-Z0-9_-]', '', str(t)) or 'general'
    dst_dir = os.path.join(templates_root, t)
    os.makedirs(dst_dir, exist_ok=True)
    import shutil
    for item in os.listdir(src_dir):
        if item in ('.backups', '.git', '__pycache__') or item.startswith('.'):
            continue
        s = os.path.join(src_dir, item)
        d = os.path.join(dst_dir, item)
        if os.path.isfile(s):
            shutil.copy2(s, d)
        elif os.path.isdir(s):
            if os.path.exists(d):
                shutil.rmtree(d, ignore_errors=True)
            shutil.copytree(s, d)
    return dst_dir, t


# ---------------------------------------------------------------------------
# Viking context — tag parser + routing (the "viking context" on the controller).
#
# The Viking plugin itself lives in viking.py (lazy, self-contained, never
# raises). This section owns the AGENT SIDE: a parser that detects the
# <vctx> / <vput> / <vfind> tags in free text and routes each one to the
# plugin, replacing the tag with its result so the answer flows straight into
# the niceai chain.
#
# Tag grammar (case-insensitive, content may span lines):
#   <vput>path, data</vput>           store `data` at `path`
#   <vctx>path, tokens</vctx>         retrieve compressed context (tokens opt.)
#   <vfind>query, tokens</vfind>      semantic search (tokens opt.)
#
# allow_write=False (used for MODEL-emitted text) still runs the read-only
# tags (vctx/vfind) but leaves any vput tag in place + flags it, so a model
# can't silently write to the store without the user's own typed tag.
# ---------------------------------------------------------------------------

VIKING_TAG_RE = re.compile(r'<(vctx|vput|vfind)>(.*?)</\1>', re.DOTALL | re.IGNORECASE)


def has_viking_tags(text: str) -> bool:
    """Fast pre-check so the hot path can skip parsing ordinary messages."""
    return bool(text) and VIKING_TAG_RE.search(text) is not None


def _parse_args(tag: str, body: str):
    """Split a tag body into (first, rest). `first` is path/query, `rest` is
    the token count (vctx/vfind) or the data payload (vput). Never raises."""
    body = (body or '').strip()
    if ',' in body:
        first, rest = body.split(',', 1)
        return first.strip(), rest.strip()
    return body, ''


def _to_int(value: str, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def process_viking_tags(text: str, allow_write: bool = True):
    """Detect + execute Viking tags in `text`. Returns (new_text, actions).

    * each <vctx>/<vfind> tag is replaced by the retrieved context string;
    * each <vput> tag is replaced by its confirmation — unless allow_write is
      False, in which case the tag is kept verbatim and only flagged;
    * actions is a list of {tag, arg, ok, result} for logging/UI.
    Never raises: a missing plugin or dead server just leaves the tag and
    records the error in actions.
    """
    if not has_viking_tags(text):
        return text, []
    try:
        import viking  # lazy — keeps boot cost zero when unused
    except Exception as e:
        return text, [{'tag': '?', 'arg': '', 'ok': False,
                       'result': f'viking plugin not importable: {e}'}]

    actions = []

    def _repl(m):
        tag = m.group(1).lower()
        body = m.group(2)
        if tag == 'vctx':
            path, toks = _parse_args(tag, body)
            out = viking.vctx(path, _to_int(toks, 300))
            actions.append({'tag': tag, 'arg': path, 'ok': True, 'result': out})
            return out
        if tag == 'vfind':
            query, toks = _parse_args(tag, body)
            out = viking.vfind(query, _to_int(toks, 500))
            actions.append({'tag': tag, 'arg': query, 'ok': True, 'result': out})
            return out
        # vput
        path, data = _parse_args(tag, body)
        if not allow_write:
            actions.append({'tag': tag, 'arg': path, 'ok': False,
                            'result': 'model-proposed vput — needs your typed tag to run'})
            return m.group(0)  # keep the tag verbatim
        out = viking.vput(path, data)
        actions.append({'tag': tag, 'arg': path, 'ok': True, 'result': out})
        return out

    new_text = VIKING_TAG_RE.sub(_repl, text)
    return new_text, actions


# ---------------------------------------------------------------------------
# build_project_context — the single source of truth for "what's in a project?"
#
# Injected into chat-model prompts by BOTH niceai.py (_project_context_for_llm)
# and aichat_xterm.py (get_project_context) so a model can answer "what files
# exist", "what tasks are outstanding", "what's the run status" from REAL disk
# data — never hallucinated. Pure file reads, no model calls, no UI.
# ---------------------------------------------------------------------------

def _human_size(n):
    if n < 1024:
        return f"{int(n)}B"
    if n < 1048576:
        return f"{n/1024:.0f}KB"
    return f"{n/1048576:.1f}MB"


def _file_age(ts):
    """Human age of a file, plus an absolute date so 'older than X days' and
    'was it modified in August?' are both answerable. Uses mtime (Linux has no
    portable creation time; mtime is the practical 'how recent' signal)."""
    import datetime
    try:
        dt = datetime.datetime.fromtimestamp(ts)
        s = int(time.time() - ts)
        if s < 60:
            age = 'just now'
        elif s < 3600:
            age = f'{s//60}m ago'
        elif s < 86400:
            age = f'{s//3600}h ago'
        elif s < 86400 * 30:
            age = f'{s//86400}d ago'
        else:
            age = f'{s//(86400*30)}mo ago'
        return f"{age} ({dt:%Y-%m-%d})"
    except Exception:
        return ''


def build_project_context(project=None, workspace_root=None):
    """Deterministic context block for a project + the workspace it lives in.

    Returns '' when no project / workspace is available. Never raises.
    """
    if not project or not workspace_root:
        return ""
    proj_dir = os.path.join(workspace_root, project)
    if not os.path.isdir(proj_dir):
        return ""

    # --- Workspace overview: all project folders ---
    try:
        projects = sorted(
            p for p in os.listdir(workspace_root)
            if os.path.isdir(os.path.join(workspace_root, p))
            and not p.startswith(('.', '_'))
            and p not in ('git', 'llama.cpp', 'tmp')
        )
    except Exception:
        projects = []
    if not projects:
        return ""

    def _pfx():
        return []
    ctx = []
    ctx.append("PROJECT STRUCTURE (deterministic, from disk — trust this over memory):")
    _proj_lines = []
    for p in projects[:15]:
        pd = os.path.join(workspace_root, p)
        try:
            kf = [f for f in sorted(os.listdir(pd))
                  if not f.startswith('.') and not f.startswith('_')
                  and f in ('index.html', 'main.py', 'app.py', 'README.md',
                            'task-order.md')][:1]
            if kf:
                _psz = _human_size(os.path.getsize(os.path.join(pd, kf[0])))
                _proj_lines.append(f"`{p}` (e.g. {kf[0]} {_psz})")
            else:
                _proj_lines.append(f"`{p}`")
        except Exception:
            _proj_lines.append(f"`{p}`")
    ctx.append(f"Workspace projects ({len(projects)} total): {'; '.join(_proj_lines)}")
    if len(projects) > 15:
        ctx[-1] += f" (+{len(projects)-15} more)"

    # --- Active project detail ---
    ctx.append(f"\nACTIVE PROJECT: {project}")

    try:
        all_files = sorted(
            f for f in os.listdir(proj_dir)
            if not f.startswith('.') and f != '__pycache__'
        )
    except Exception:
        all_files = []
    visible = [f for f in all_files
               if f not in ('test.log', 'state.json', 'project_config.json',
                            'RUNTIME.md')]

    # --- Task order (authoritative status) ---
    to_path = os.path.join(proj_dir, 'task-order.md')
    tasks = []
    if os.path.isfile(to_path):
        try:
            with open(to_path, 'r', encoding='utf-8', errors='replace') as f:
                to_text = f.read()
            to_text, _repairs = repair_malformed_checkboxes(to_text)
            tasks = parse_task_order(to_text)
        except Exception:
            tasks = []
    if tasks:
        done_n = sum(1 for t in tasks if t.get('done'))
        pending = [t for t in tasks if not t.get('done')
                   and not t.get('pending') and not t.get('obsolete')]
        parked = [t for t in tasks if t.get('pending')]
        obsolete_n = sum(1 for t in tasks if t.get('obsolete'))
        ctx.append(
            f"Task order: {done_n} done, {len(pending)} outstanding"
            + (f", {len(parked)} parked (#tmp)" if parked else "")
            + (f", {obsolete_n} obsolete" if obsolete_n else "")
        )
        # Pending + parked in full (these are actionable); done collapses to
        # a count + last 3 so a 22-done project doesn't bloat every prompt.
        for i, t in enumerate(pending, 1):
            ctx.append(f"  #{i} [pending] {(t.get('label') or '').strip()[:140]}")
        for i, t in enumerate(parked, 1):
            ctx.append(f"  [parked {i}] {(t.get('label') or '').strip()[:140]}")
        if done_n:
            dones = [t for t in tasks if t.get('done')]
            ctx.append(f"  ({done_n} done, most recent:")
            for t in dones[-3:]:
                ctx.append(f"    - {(t.get('label') or '').strip()[:100]}")
            ctx.append("  )")
        if done_n and not pending and not parked:
            ctx.append("  (all milestones complete!)")
    else:
        ctx.append("(no task-order.md)")

    # --- Run state (live status / last milestone) ---
    rs_path = os.path.join(proj_dir, 'run_state.json')
    if os.path.isfile(rs_path):
        try:
            with open(rs_path, 'r', encoding='utf-8', errors='replace') as f:
                rs = json.loads(f.read())
            status = rs.get('status', '?')
            stage = rs.get('current_stage', '')
            done_n = rs.get('tasks_done', 0)
            total_n = rs.get('tasks_total', 0)
            ctx.append(
                f"Run state: {status}"
                + (f" (stage {stage})" if stage else "")
                + f", {done_n}/{total_n} milestones"
            )
            if rs.get('current_milestone'):
                ctx.append(f"  current milestone: {rs['current_milestone'][:140]}")
        except Exception:
            pass

    # --- File listing (.nav.md preferred, live fallback) ---
    nav = read_nav_file(proj_dir)
    if nav:
        ctx.append(f"Files (from {NAV_FILENAME}):\n{nav}")
        rest = []
    else:
        _JOURNALS = ('task-order.md', 'task.md', 'log.md', 'run_state.json',
                     'state.json', 'test.log', 'project_config.json',
                     'RUNTIME.md', '.nav.md')
        rest = [f for f in visible if f not in _JOURNALS]
        
        if rest:
            total_line = (f"Files ({len(rest)} total — answer 'how many files' "
                          f"with this number, do not recount; names in `backticks` — copy exactly):")
            ctx.append(total_line)
            for f in rest[:20]:
                fp = os.path.join(proj_dir, f)
                try:
                    st = os.stat(fp) if os.path.isfile(fp) else None
                    sz = st.st_size if st else 0
                    age = _file_age(st.st_mtime) if st else ''
                    seg = f"`{f}` ({_human_size(sz)}"
                    if age:
                        seg += f", {age}"
                    seg += ")"
                    ctx.append(f"  - {seg}")
                except Exception:
                    ctx.append(f"  - `{f}`")
            if len(rest) > 20:
                ctx.append(f"  ({len(rest)-20} more files on disk)")

    ctx.append("\nAnswer questions about projects/tasks from the above only.")
    return '\n'.join(ctx)


# ---------------------------------------------------------------------------
# build_root_index — always-on orientation block for aichat_xterm.py.
#
# Cheap (~8 lines) and UNGATED: injected on every prompt so the model always
# knows WHERE things live, regardless of phrasing. Tags are stable vocabulary
# the roles/rules can reference ([PROJECTS], [RECORDS], ...).
#
# Why not aichat's native `-f <dir>`: `-f` recursively DUMPS full file
# contents (verified: dir + nested + PDF text all inlined). On WORKSPACE that
# is a token blowup on this box. Deterministic index here; native `-f` only
# for single doc files (see DOC_ATTACH_EXTS + find_doc_attach below).
# ---------------------------------------------------------------------------

# tag -> (relative path, one-line meaning). Edit here, not in prompts.
ROOT_TAGS = {
    'PROJECTS': ('WORKSPACE', 'main project folders (each subfolder = one project)'),
    'RECORDS': ('History', 'niceai session records + install scripts'),
    'TEMPLATES': ('WORKSPACE/_templates', 'starter templates for new projects'),
    'BACKUPS': ('backup', 'code backups (.bak files)'),
}

# Read by aichat `-f` directly (verified live: PDF text extracted, csv/html/py
# as text). Office binaries are NOT in this list — they convert first (below).
DOC_ATTACH_EXTS = ('.pdf', '.md', '.txt', '.json', '.csv', '.py', '.html',
                   '.js', '.css', '.xml', '.yaml', '.yml', '.log', '.sh')

# Office formats needing stdlib conversion (aichat `-f` fails on them without
# pandoc, which this box doesn't have). NOTE: '.doc'/'.xls'/'.ppt' (old binary
# OLE) deliberately excluded — stdlib can't parse them, and "doc" collides
# with the conversational word "doc(ument)".
OFFICE_EXTS = ('.docx', '.xlsx', '.pptx')
ALL_ATTACH_EXTS = DOC_ATTACH_EXTS + OFFICE_EXTS

# Words that must NEVER match a file name on their own. Without this, a filename
# token like "the" (from "grouping-the-gang.py") matches the prompt word "the",
# and the whole source file is silently attached to every turn — which is exactly
# the "Exceed max_input_tokens" / junk-context loop.
_STOPWORDS = frozenset((
    'the', 'a', 'an', 'and', 'or', 'of', 'to', 'in', 'on', 'for', 'with', 'is',
    'are', 'was', 'were', 'be', 'been', 'this', 'that', 'these', 'those', 'it',
    'its', 'as', 'at', 'by', 'from', 'me', 'my', 'we', 'our', 'you', 'your',
    'he', 'she', 'they', 'them', 'his', 'her', 'i', 'am', 'do', 'does', 'did',
    'what', 'which', 'who', 'whom', 'how', 'when', 'where', 'why', 'give', 'get',
    'find', 'show', 'read', 'extract', 'body', 'folder', 'file', 'files',
    'project', 'please', 'can', 'could', 'would', 'should', 'will', 'shall',
    'summary', 'summarize', 'summarise', 'summar', 'about', 'over', 'under',
    'more', 'text', 'content', 'contents', 'document', 'doc', 'poc', 'point',
    'contact', 'info',
))


def office_to_text(path):
    """Stdlib-only .docx/.xlsx/.pptx -> text. Returns str or None. Never raises."""
    import zipfile as _zf
    import re as _re
    try:
        name = path.lower()
        with _zf.ZipFile(path) as z:
            names = z.namelist()
            if name.endswith('.docx'):
                parts = [n for n in names if n.startswith('word/') and n.endswith('.xml')]
                out = []
                for n in parts:
                    xml = z.read(n).decode('utf-8', 'replace')
                    out.extend(_re.findall(r'<w:t[^>]*>(.*?)</w:t>', xml))
                return '\n'.join(out) or None
            if name.endswith('.xlsx'):
                ss = []
                try:
                    sxml = z.read('xl/sharedStrings.xml').decode('utf-8', 'replace')
                    ss = _re.findall(r'<t[^>]*>(.*?)</t>', sxml)
                except KeyError:
                    pass
                out = []
                for n in names:
                    if '/worksheets/' in n and n.endswith('.xml'):
                        xml = z.read(n).decode('utf-8', 'replace')
                        for m in _re.finditer(r'<c\b([^>]*)>(.*?)</c>', xml):
                            tag, inner = m.group(1), m.group(2)
                            if 't="s"' in tag:
                                try:
                                    out.append(ss[int(_re.search(r'<v>(\d+)</v>', inner).group(1))])
                                except Exception:
                                    pass
                            else:
                                vm = _re.search(r'<v>(.*?)</v>', inner)
                                im = _re.search(r'<t[^>]*>(.*?)</t>', inner)
                                out.append(vm.group(1) if vm else (im.group(1) if im else ''))
                return '\n'.join(t for t in out if t) or None
            if name.endswith('.pptx'):
                out = []
                for n in names:
                    if '/slides/slide' in n and n.endswith('.xml'):
                        xml = z.read(n).decode('utf-8', 'replace')
                        out.append(f"--- {n.split('/')[-1]} ---")
                        out.extend(_re.findall(r'<a:t>(.*?)</a:t>', xml))
                return '\n'.join(out) or None
    except Exception:
        return None
    return None


def build_root_index(base_dir=None):
    """Compact always-on root orientation block. Never raises."""
    import os as _os
    if not base_dir:
        return ""
    try:
        root = _os.path.abspath(str(base_dir))
        ws = _os.path.join(root, 'WORKSPACE')
        try:
            projs = sorted(
                p for p in _os.listdir(ws)
                if _os.path.isdir(_os.path.join(ws, p))
                and not p.startswith(('.', '_'))
            )
        except Exception:
            projs = []
        lines = ["ROOT INDEX (stable locations — always true, use the [tags]):"]
        lines.append(f"[ROOT] {root} = app home")
        shown = ', '.join(f"`{p}`" for p in projs[:20]) + (f" (+{len(projs)-20} more)" if len(projs) > 20 else "")
        lines.append(f"[PROJECTS] WORKSPACE/ = main project folders ({len(projs)}): {shown or '(empty)'}")
        for tag, (rel, meaning) in ROOT_TAGS.items():
            if tag == 'PROJECTS':
                continue
            lines.append(f"[{tag}] {rel}/ = {meaning}")
        return '\n'.join(lines)
    except Exception:
        return ""


def find_doc_attach(prompt, base_dir=None, max_files=3, max_bytes=1000000):
    """Named doc files under base_dir the prompt asks to read/extract.

    Returns [paths] for aichat `-f`. Single files only, size-capped — never
    a whole directory (that would dump WORKSPACE into tokens). Never raises.
    """
    import os as _os
    import re as _re
    if not prompt or not base_dir:
        return []
    p = prompt.lower()
    # STRICT: only attach when the prompt EXPLICITLY names a file (a token with a
    # recognized extension, e.g. "proposal.md", "rfp.pdf"). Fuzzy word-matching
    # kept attaching source files to general chat ("context" -> plan_context.py,
    # "the" -> grouping-the-gang.py, "summary" -> session_summary.md) — the #1
    # off-topic/hallucination driver on small models.
    _NAMED_RE = _re.compile(
        r'[\w\-\.]+\.(?:md|txt|pdf|docx|xlsx|pptx|csv|json|html?|py|js|css|yaml|yml|log|sh)\b')
    named = set()
    for _m in _NAMED_RE.finditer(p):
        _stem = _m.group(0).rsplit('.', 1)[0]
        if _stem and _stem not in _STOPWORDS:
            named.add(_stem)
    _bare_ok = any(v in p for v in ('summar', 'explain', 'describe', 'extract',
                                    'attach', 'read this', 'read the'))
    if not named and not _bare_ok:
        return []
    root = _os.path.abspath(str(base_dir))
    found = []
    # project scoping: a real WORKSPACE folder named in the prompt wins over
    # same-named files in other projects ("find the rfp in projB").
    ws = _os.path.join(root, 'WORKSPACE')
    scoped = ""
    try:
        for _pn in _os.listdir(ws):
            if _os.path.isdir(_os.path.join(ws, _pn)) and _pn.lower() in p:
                scoped = _os.path.join(ws, _pn)
                break
    except Exception:
        pass
    try:
        # walk cheap: top 2 levels of WORKSPACE + root files
        cands = []
        for dp, dns, fns in _os.walk(root):
            depth = _os.path.relpath(dp, root).count(_os.sep)
            if depth > 2 or any(_x in dp for _x in
                                 ('/venv_ui', '/.git', '/History', '/backup',
                                  '/Models', '/.backups', '/__pycache__')):
                dns[:] = []
                continue
            for fn in fns:
                if fn.lower().endswith(ALL_ATTACH_EXTS):
                    cands.append(_os.path.join(dp, fn))
        if scoped:
            cands.sort(key=lambda c: (not c.startswith(scoped), c))
        _words = set(_re.findall(r'[a-z0-9_\-]+', p)) if not named else set()
        for c in cands:
            base = _os.path.basename(c).lower()
            stem = _os.path.splitext(base)[0]
            if named:
                # EXACT stem match against the explicitly named file(s) only.
                if not any(n == stem or n in stem or stem in n for n in named):
                    continue
            else:
                # Bare-stem pass (no extension typed, e.g. "summarize
                # cheatsheet"): whole-word EXACT stem equality only — never
                # substring, so "summary" can't match "session_summary".
                if stem not in _words:
                    continue
            try:
                if _os.path.getsize(c) > max_bytes:
                    continue
                if c.lower().endswith(OFFICE_EXTS):
                    # native `-f` can't read office binaries (no pandoc):
                    # convert to a .txt sidecar and attach that instead.
                    txt = office_to_text(c)
                    if not txt:
                        continue
                    side = _os.path.join(
                        '/tmp', 'aichat_docs',
                        _os.path.basename(c) + '.txt')
                    _os.makedirs('/tmp/aichat_docs', exist_ok=True)
                    with open(side, 'w', encoding='utf-8') as f:
                        f.write(f"SOURCE FILE: {c}\n\n{txt}")
                    found.append(side)
                else:
                    found.append(c)
            except Exception:
                pass
            if len(found) >= max_files:
                break
    except Exception:
        return []
    return found


# ---------------------------------------------------------------------------
# build_here_index — "where am I" block (always on, like the root index).
#
# The tree click sets the working folder; the model must answer "at THIS
# level" from it, not default to [PROJECTS]. Pre-counted + backticked, same
# treatment as everything else. Never raises.
# ---------------------------------------------------------------------------

def build_here_index(workdir=None):
    import os as _os
    if not workdir or not _os.path.isdir(str(workdir)):
        return ""
    try:
        root = _os.path.abspath(str(workdir))
        entries = sorted(_os.listdir(root))
        dirs = [e for e in entries
                if _os.path.isdir(_os.path.join(root, e)) and not e.startswith('.')]
        nfiles = sum(1 for e in entries
                     if _os.path.isfile(_os.path.join(root, e)) and not e.startswith('.'))
        lines = [f"YOU ARE HERE: {root}"]
        shown = ', '.join(f"`{d}`" for d in dirs[:30])
        if len(dirs) > 30:
            shown += f" (+{len(dirs)-30} more)"
        lines.append(f"Folders at this level ({len(dirs)} total): {shown or '(none)'}")
        lines.append(f"Files at this level: {nfiles} total")
        lines.append("Scope: only these — do not descend into subfolders or other levels.")
        return '\n'.join(lines)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# .nav.md — per-folder navigation file (preferred) + improved live fallback.
#
# Rule: if <proj>/.nav.md exists, its file-list section is used as-is;
# otherwise the live listing below is built. Either way the model NEVER
# counts or respells: totals are pre-computed by Python and every name is
# wrapped in `backticks` on its own line (small models copy code spans far
# more faithfully than comma-joined prose, where they eat underscores).
# ---------------------------------------------------------------------------

NAV_FILENAME = '.nav.md'


def read_nav_file(proj_dir):
    """Return .nav.md content or None. Never raises."""
    try:
        p = os.path.join(proj_dir, NAV_FILENAME)
        if os.path.isfile(p):
            with open(p, 'r', encoding='utf-8', errors='replace') as f:
                return f.read()[:2000]
    except Exception:
        pass
    return None


def write_nav_file(proj_dir, purpose=""):
    """(Re)generate .nav.md from live disk state. Returns path or None."""
    import datetime as _dt
    # Same journal exclusions as the live fallback so both paths agree.
    _JOURNALS = ('task-order.md', 'task.md', 'log.md', 'run_state.json',
                 'state.json', 'test.log', 'project_config.json', 'RUNTIME.md')
    try:
        try:
            files = sorted(
                f for f in os.listdir(proj_dir)
                if not f.startswith('.') and f != '__pycache__'
                and f not in _JOURNALS
            )
        except Exception:
            files = []
        lines = [f"# NAV: {os.path.basename(proj_dir)}",
                 f"updated: {_dt.datetime.now():%Y-%m-%d %H:%M}",
                 f"TOTAL FILES: {len(files)} (answer 'how many files' with this number — do not recount)"]
        if purpose:
            lines.append(f"purpose: {purpose}")
        lines.append("")
        for f in files[:40]:
            fp = os.path.join(proj_dir, f)
            try:
                if os.path.isdir(fp):
                    lines.append(f"- `{f}`/ (folder)")
                else:
                    sz = os.path.getsize(fp)
                    lines.append(f"- `{f}` ({_human_size(sz)})")
            except Exception:
                lines.append(f"- `{f}`")
        if len(files) > 40:
            lines.append(f"({len(files)-40} more entries on disk)")
        path = os.path.join(proj_dir, NAV_FILENAME)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        return path
    except Exception:
        return None
def vctx(path, tokens=300):
    import viking
    return viking.vctx(path, tokens)


def vput(path, data):
    import viking
    return viking.vput(path, data)


def vfind(query, tokens=500):
    import viking
    return viking.vfind(query, tokens)


def viking_status():
    import viking
    return viking.status()
