import asyncio
import logging
import os
import re
import shutil
import sys
from pathlib import Path
import time
from typing import Optional
from nicegui import app, ui

# Self-hosted draw.io editor (offline, Apache-2.0). Served locally so the flow
# board's "edit in draw.io" never depends on the cloud editor. The assets/drawio
# folder (150MB) ships separately — skip silently when absent, mount when present.
_drawio_dir = Path(__file__).resolve().parent / 'assets' / 'drawio'
if _drawio_dir.is_dir():
    try:
        app.add_static_files('/drawio', str(_drawio_dir))
    except Exception as _e:
        logging.warning('drawio static mount failed: %s', _e)

# Global draw.io <-> parent postMessage bridge. Registered ONCE at page load (not
# per-dialog) so it is always in place before the iframe posts its `init` event.
# `window._drawioLoadXml` is set by _open_drawio_editor before the iframe mounts.
ui.add_body_html('''
<script>
window._drawioXml = null;
window._drawioLoadXml = null;

window.addEventListener('message', function(e) {

    var d = null;

    try {
        d = typeof e.data === 'string'
            ? JSON.parse(e.data)
            : e.data;
    } catch (err) {
        console.warn('[DRAWIO] bad message:', e.data);
        return;
    }

    if (!d) return;

    console.log('[DRAWIO] MESSAGE:', d);

    if (d.event === 'init') {

        console.log('[DRAWIO] ===== INIT =====');

        var f = document.getElementById('drawio-frame');

        console.log('[DRAWIO] iframe:', f);
        console.log(
            '[DRAWIO] XML ready:',
            !!window._drawioLoadXml
        );

        if (
            f &&
            f.contentWindow &&
            window._drawioLoadXml
        ) {

            console.log('[DRAWIO] sending LOAD from INIT');

            f.contentWindow.postMessage(
                JSON.stringify({
                    action: 'load',
                    xml: window._drawioLoadXml,
                    autosave: 1
                }),
                '*'
            );

        } else {

            console.warn(
                '[DRAWIO] cannot send LOAD yet'
            );

        }

    } else if (
        d.event === 'save' ||
        d.event === 'autosave'
    ) {

        console.log('[DRAWIO] SAVE received');

        window._drawioXml = d.xml || null;

        console.log(
            '[DRAWIO] saved XML length:',
            window._drawioXml
                ? window._drawioXml.length
                : 0
        );
    }

});
</script>
''')


# ============================================================
# MERMAID FLOW BOARD
# ============================================================

def _mermaid_js() -> str:
    """Local Mermaid UMD (no CDN). Inlined so the flow board renders offline and
    never depends on the browser reaching jsdelivr — the ESM CDN import was the
    "mermaid not showing up" culprit (works in curl but not reliably in-browser)."""
    try:
        p = Path(__file__).resolve().parent / 'assets' / 'mermaid.min.js'
        return p.read_text(encoding='utf-8')
    except Exception as e:
        logging.warning('mermaid asset read failed: %s', e)
        return ''


_mermaid_js = _mermaid_js()
if _mermaid_js:
    ui.add_head_html(
        '<script>' + _mermaid_js + '</script>'
        "<script>window.mermaid.initialize("
        "{startOnLoad:false,theme:'dark',securityLevel:'loose'});</script>"
    )

# Draggable popup windows: every dialog card with class `dialog-drag` + a
# `drag-handle` on its title can be moved (mouse + touch). Grab the title.
ui.add_head_html(
    """
<style>
.dialog-drag .drag-handle { cursor: grab; user-select: none; }
.dialog-drag .drag-handle:active { cursor: grabbing; }
.dialog-drag { resize: both; overflow: auto; min-width: 280px; min-height: 200px; max-width: 98vw; max-height: 96vh; }
.dialog-drag.fill-card { display: flex; flex-direction: column; }
.fill-textarea, .fill-textarea .q-field__inner, .fill-textarea .q-field__control { height: 100%; }
.fill-textarea textarea { height: 100% !important; min-height: 50vh; resize: none; }
</style>
<script>
(function () {
  // Array (not {}): element keys stringify to "[object HTMLDivElement]" and
  // collide, so every dragged window shared one slot. Array keeps per-card pos.
  window._dragState = [];
  function _slot(card) {
    for (var i = 0; i < window._dragState.length; i++) {
      if (window._dragState[i].el === card) return window._dragState[i];
    }
    var s = { el: card, left: 0, top: 0 };
    window._dragState.push(s);
    return s;
  }
  function applyPos(card) {
    var st = _slot(card);
    if (!st || !st.left) return;
    card.style.position = 'fixed';
    card.style.margin = '0';
    card.style.left = st.left + 'px';
    card.style.top = st.top + 'px';
    card.style.transform = 'none';
  }
  function beginDrag(card, startX, startY, touch) {
    var p = card.parentElement;
    while (p && p !== document.body) {
      try { p.style.transform = 'none'; } catch (e) {}
      p = p.parentElement;
    }
    var cur = card.getBoundingClientRect();
    card.style.position = 'fixed';
    card.style.margin = '0';
    card.style.left = cur.left + 'px';
    card.style.top = cur.top + 'px';
    card.style.transform = 'none';
    card.style.zIndex = '9500';
    var sl = _slot(card); sl.left = cur.left; sl.top = cur.top;
    var ox = cur.left, oy = cur.top, sx = startX, sy = startY;
    function move(cx, cy) {
      var L = ox + (cx - sx), T = oy + (cy - sy);
      card.style.left = L + 'px'; card.style.top = T + 'px';
      var s2 = _slot(card); s2.left = L; s2.top = T;
    }
    function stop() {
      if (touch) {
        window.removeEventListener('touchmove', tm); window.removeEventListener('touchend', stop);
      } else {
        window.removeEventListener('mousemove', mm); window.removeEventListener('mouseup', stop);
      }
    }
    function mm(ev) { if (ev.preventDefault) ev.preventDefault(); move(ev.clientX, ev.clientY); }
    function tm(ev) {
      if (ev.touches && ev.touches.length) { ev.preventDefault(); move(ev.touches[0].clientX, ev.touches[0].clientY); }
    }
    if (touch) { window.addEventListener('touchmove', tm, { passive: false }); window.addEventListener('touchend', stop); }
    else { window.addEventListener('mousemove', mm); window.addEventListener('mouseup', stop); }
  }
  function onDown(e) {
    if (!e.target || !e.target.closest) return;
    if (!e.target.closest('.drag-handle')) return;
    var card = e.target.closest('.dialog-drag') || e.target.closest('.q-card');
    if (!card) return;
    e.preventDefault();
    beginDrag(card, e.clientX, e.clientY, false);
  }
  function onTouchStart(e) {
    if (!e.target || !e.target.closest) return;
    if (!e.target.closest('.drag-handle')) return;
    var card = e.target.closest('.dialog-drag') || e.target.closest('.q-card');
    if (!card) return;
    e.preventDefault();
    var t = e.touches && e.touches[0];
    if (t) beginDrag(card, t.clientX, t.clientY, true);
  }
  document.addEventListener('mousedown', onDown, true);
  document.addEventListener('touchstart', onTouchStart, { passive: false, capture: true });
  // Head scripts run before <body> exists — document.body is null here, which
  // threw "MutationObserver.observe: Argument 1 is not an object" at boot.
  // Defer until the body is present, then re-assert dragged positions when
  // Quasar re-renders (it resets transforms, snapping windows back).
  function setupMo() {
    if (!document.body) { setTimeout(setupMo, 50); return; }
    var mo = new MutationObserver(function () {
      for (var i = 0; i < window._dragState.length; i++) {
        try {
          var el = window._dragState[i].el;
          if (el && el.isConnected) applyPos(el);
        } catch (e) {}
      }
    });
    mo.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['style', 'class'] });
  }
  setupMo();
})();
</script>
"""
)

import threading

from aichat_project_controller import (
    process_viking_tags,
    has_viking_tags,
    build_project_context,
    build_root_index,
    build_here_index,
    find_doc_attach,
    read_nav_file,
    parse_task_order,
    parse_flow_containers,
)

from aichat_docs import (
    extract_pdf_text,
    pdf_sidecar,
    resolve_doc_file,
    doc_page_lookup,
    read_text_capped,
    suggest_commands,
    parse_pdf_args,
    pdf_page_paragraphs,
    COMMANDS,
)


try:
    import aichat_model_controller as _mc
    HAS_MC = True
except Exception as _mc_err:
    logging.warning("model controller unavailable: %s", _mc_err)
    HAS_MC = False
    _mc = None

try:
    import auth_store as _vault
    HAS_VAULT = True
except Exception as _vault_err:
    logging.warning("vault unavailable: %s", _vault_err)
    HAS_VAULT = False
    _vault = None

# ============================================================
# HARMONY AI TERMINAL
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

ui.dark_mode().enable()

BASE_DIR = Path(__file__).resolve().parent  # portable: runs from any folder

AICHAT_CONFIG = Path.home() / '.config' / 'aichat' / 'config.yaml'

SETTINGS_PATH = BASE_DIR / 'aichat_settings.json'
_PERSIST_KEYS = ('role', 'hat', 'active_project', 'workdir')


def _aichat_bin() -> str:
    """Resolve the aichat CLI robustly (PATH, then ~/.local/bin fallback).

    The panel launches aichat as a subprocess; if the shell that started the
    panel lacks ~/.local/bin on PATH, plain 'aichat' fails with
    "No such file or directory" and the model never answers. Fallback to the
    known install path so a chat turn can never die on PATH alone.
    """
    found = shutil.which('aichat')
    if found:
        return found
    fb = Path.home() / '.local' / 'bin' / 'aichat'
    if fb.is_file():
        return str(fb)
    return 'aichat'

app_state = {
    "selected_file": None,
    "workdir": str(BASE_DIR),
    "role": "brainstorm",
    "process": None,
    "viking_client": None,
    "active_project": None,
    "hat": "EXPLORE",
    "last_doc": None,
}


def save_settings():
    """Persist the user's working state (role/hat/project/workdir) so a restart
    comes back exactly as it was. Never raises."""
    import json
    try:
        data = {k: app_state.get(k) for k in _PERSIST_KEYS}
        SETTINGS_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        logging.warning('save_settings failed: %s', e)


def load_settings():
    """Restore role/hat/project/workdir from aichat_settings.json. Never raises."""
    import json
    try:
        if not SETTINGS_PATH.is_file():
            return
        data = json.loads(SETTINGS_PATH.read_text(encoding='utf-8'))
        for k in _PERSIST_KEYS:
            if k in data and data[k] is not None:
                app_state[k] = data[k]
    except Exception as e:
        logging.warning('load_settings failed: %s', e)


load_settings()


def safe_notify(msg, type='positive', timeout=5000):
    """ui.notify that never raises — background threads must not crash when the
    browser tab is closed/deleted (NiceGUI 'Client has been deleted')."""
    try:
        ui.notify(msg, type=type, timeout=timeout)
    except Exception:
        pass


def safe_update(el):
    """Call el.update() without raising (element gone after client deletion)."""
    try:
        el.update()
    except Exception:
        pass


# ============================================================
# FLOW BOARD UI
# ============================================================

flow_dialog = ui.dialog()

with flow_dialog:
    with ui.card().classes(
        'w-[92vw] h-[88vh] bg-[#0d1117] '
        'border border-emerald-900 shadow-2xl dialog-drag'
    ):
        with ui.row().classes(
            'w-full items-center justify-between'
        ):
            ui.label(
                '📐 PROJECT FLOW'
            ).classes(
                'text-xl font-bold text-emerald-400 drag-handle'
            )

            with ui.row().classes('gap-1'):
                ui.button(
                    '✏️ Edit',
                    on_click=lambda: _open_flow_editor()
                ).props(
                    'flat dense color=sky'
                ).tooltip('Edit flow.md (Mermaid)')

                ui.button(
                    '🖊️ draw.io',
                    on_click=lambda: _open_drawio_editor()
                ).props(
                    'flat dense color=orange'
                ).tooltip('Edit the flow visually (self-hosted draw.io)')

                ui.button(
                    '➕ Generate',
                    on_click=lambda: _generate_flow_click()
                ).props(
                    'flat dense color=amber'
                ).tooltip('Generate flow.md from this project\'s task-order.md')

                ui.button(
                    icon='refresh',
                    on_click=lambda: render_flow_board()
                ).props(
                    'flat dense color=emerald'
                ).tooltip('Reload flow.md')

                ui.button(
                    icon='close',
                    on_click=flow_dialog.close
                ).props(
                    'flat dense color=gray'
                ).tooltip('Close Flow Board')

        ui.separator().classes('bg-emerald-900')

        flow_board = ui.html(
            '''
            <div id="flow-board"
                 style="
                    width:100%;
                    min-height:60vh;
                    overflow:auto;
                    padding:24px;
                    display:flex;
                    justify-content:center;
                    align-items:flex-start;
                 ">
                <div style="
                    color:#6b7280;
                    padding:40px;
                    font-family:monospace;
                 ">
                    Flow Board
                </div>
            </div>
            '''
        ).classes(
            'w-full flex-grow overflow-hidden'
        )

# ============================================================
# ROLE COLORS (single source of truth: buttons + chat label match)
# ============================================================

_RAINBOW_BG = ('linear-gradient(90deg,#ff0000,#ff9900,'
               '#ffee00,#33cc33,#3399ff,#cc66ff)')

# NOTE: Quasar/Tailwind bg classes are !important, so role backgrounds MUST
# carry !important too or the buttons all render the same color. Every lane
# keeps its own color ALWAYS (active = full + glow, inactive = dimmed).
_ROLE_BTN_BG = {
    'brainstorm': f'background:{_RAINBOW_BG}!important',
    'plan': 'background:#2563eb!important',
    'exec': 'background:#dc2626!important',
    'qc': 'background:#16a34a!important',
}

# White text + tiny dark shade so it protrudes on bright gradients.
_BTN_TEXT = 'color:#fff;font-weight:bold;text-shadow:1px 1px 2px rgba(0,0,0,.85)'
_ACTIVE_FX = 'opacity:1;box-shadow:0 0 8px rgba(255,255,255,.45)'
_INACTIVE_FX = 'opacity:.55;box-shadow:none'

_ROLE_LABEL_STYLE = {
    'brainstorm': (f'background:{_RAINBOW_BG};-webkit-background-clip:text;'
                   'background-clip:text;color:transparent;font-weight:bold;'
                   'font-size:13px'),
    'plan': 'color:#60a5fa;font-weight:bold;font-size:13px',
    'exec': 'color:#ef4444;font-weight:bold;font-size:13px',
    'qc': 'color:#22c55e;font-weight:bold;font-size:13px',
}

_ROLE_INITIAL = {
    'brainstorm': 'B',
    'plan': 'P',
    'exec': 'E',
    'qc': 'QC',
}

_USER_COLOR = '#7dd3fc'  # user requests render in light blue

# ============================================================
# PROJECT FLOW
# ============================================================

FLOW_FILENAMES = (
    "flow.md",
    "FLOW.md",
    "flow.mmd",
)


def find_flow_file(project_dir: Optional[str] = None) -> Optional[Path]:
    """Return the project's optional flow file, if one exists."""
    base = Path(project_dir or app_state.get("workdir") or BASE_DIR)

    for name in FLOW_FILENAMES:
        path = base / name
        if path.is_file():
            return path

    return None


def read_flow(project_dir: Optional[str] = None) -> str:
    """Read the project's optional Mermaid flow."""
    path = find_flow_file(project_dir)
    if not path:
        return ""

    try:
        return path.read_text(encoding="utf-8")
    except Exception as ex:
        logging.warning("Could not read flow file %s: %s", path, ex)
        return ""

# ============================================================
# APPLICATION STATE (defined above, near _aichat_bin)
# ============================================================

# ============================================================
# FLOW BOARD RENDERER
# ============================================================

async def render_flow_board():
    """Read the current project's flow.md and render it with Mermaid."""
    try:
        flow_content = read_flow(_project_dir_for_flow())

        if not flow_content.strip():
            ui.notify(
                'No flow.md found or it is empty.',
                type='warning'
            )
            return

        # Allow both plain Mermaid files and ```mermaid fenced files.
        mermaid_text = flow_content.strip()

        if mermaid_text.startswith('```'):
            mermaid_text = re.sub(
                r'^```(?:mermaid)?\s*',
                '',
                mermaid_text,
                flags=re.IGNORECASE
            )
            mermaid_text = re.sub(
                r'\s*```$',
                '',
                mermaid_text
            ).strip()

        import json

        js_data = json.dumps(mermaid_text)

        # Unique render id per call — mermaid.render() keys its temp node by
        # this id, and a FIXED id silently fails on re-render (the board just
        # stops updating after the first click). Time-based id fixes that.
        render_id = f'harmony_flow_{int(time.time() * 1000) % 1000000}'

        await ui.run_javascript(
            f'''
            (async () => {{
                const board =
                    document.getElementById('flow-board');

                if (!board) {{
                    console.error(
                        'Harmony Flow: flow-board not found'
                    );
                    return;
                }}

                async function doRender() {{
                    try {{
                        const result =
                            await window.mermaid.render(
                                '{render_id}',
                                {js_data}
                            );
                        board.innerHTML = result.svg;
                    }} catch (err) {{
                        console.error(
                            'Harmony Flow Mermaid error:',
                            err
                        );
                        board.innerHTML = `
                            <div style="
                                color:#f87171;
                                padding:30px;
                                white-space:pre-wrap;
                                font-family:monospace;
                            ">
                                Mermaid error:

                                ${{err.message || err}}

                                --- raw flow.md ---
                                ${{JSON.stringify({js_data})}}
                            </div>
                        `;
                    }}
                }}

                if (window.mermaid) {{
                    await doRender();
                    return;
                }}

                // ESM import is async — poll up to ~3s so a render fired
                // right at page load doesn't silently come up blank.
                let tries = 0;
                const poll = async () => {{
                    if (window.mermaid) {{ await doRender(); return; }}
                    if (++tries > 30) {{
                        board.innerHTML = `
                            <div style="
                                color:#f87171;
                                padding:30px;
                                white-space:pre-wrap;
                                font-family:monospace;
                            ">
                                Mermaid.js is not loaded (no internet/CDN?).
                                --- raw flow.md ---
                                ${{JSON.stringify({js_data})}}
                            </div>
                        `;
                        return;
                    }}
                    setTimeout(poll, 100);
                }};
                poll();
            }})()
            '''
        )

    except Exception as ex:
        logging.exception("Flow board render failed")
        ui.notify(
            f'Flow render failed: {ex}',
            type='negative'
        )


def _project_dir_for_flow() -> Path:
    """The project folder the Flow Board targets: the selected project folder,
    else the current working dir, else BASE_DIR. Never raises."""
    proj = app_state.get("active_project")
    if proj:
        p = BASE_DIR / 'WORKSPACE' / proj
        if p.is_dir():
            return p
    wd = app_state.get("workdir")
    if wd and Path(wd).is_dir():
        return Path(wd)
    return BASE_DIR


def _sanitize_flow_label(s):
    s = (s or '').strip()
    s = re.sub(r'\*\*?', '', s)   # markdown bold/italic
    s = re.sub(r'`', '', s)        # inline code ticks
    s = s.replace('"', "'").replace('[', '(').replace(']', ')')
    s = re.sub(r'\s+', ' ', s).strip()
    if len(s) > 80:
        s = s[:77] + '...'
    return s


def _flow_mermaid_containers(containers) -> str:
    """Render flow containers (Goal/Deps/Planning/Execution/Done) as Mermaid
    subgraph tiles, linked in order. Each container = one tile; its tasks are
    the nodes inside."""
    icons = {'goal': '🎯', 'dependencies': '🔗', 'planning': '🗺️',
             'execution': '⚙️', 'done': '✅', 'review': '🔍', 'blocked': '🚫'}
    lines = ['flowchart TD', '']
    prev_c = None
    for ci, (cname, tasks) in enumerate(containers):
        cid = f'C{ci}'
        ic = icons.get(cname.lower(), '📌')
        lines.append(f'  subgraph {cid} [{ic} {cname}]')
        for ti, (done, label) in enumerate(tasks, 1):
            nid = f'{cid}T{ti}'
            mark = '✅' if done else '⬜'
            lbl = _sanitize_flow_label(label)
            lines.append(f'    {nid}["{mark} {lbl}"]')
        lines.append('  end')
        if prev_c is not None:
            lines.append(f'  {prev_c} --> {cid}')
        prev_c = cid
    return '\n'.join(lines) + '\n'


def _flow_mermaid_from_task_order(project_dir: Path) -> str:
    """Build a Mermaid flowchart from a project's task-order.md. Empty string
    when there is no task-order. Never raises.

    If the file uses FLOW CONTAINERS (`## Goal` / `## Dependencies` /
    `## Planning` / `## Execution` / `## Done`), render those as linked tiles.
    Otherwise fall back to a flat chain of milestones."""
    to = project_dir / 'task-order.md'
    if not to.is_file():
        return ""
    try:
        text = to.read_text(encoding='utf-8')
    except Exception:
        return ""

    containers = parse_flow_containers(text)
    if containers:
        return _flow_mermaid_containers(containers)

    tasks = parse_task_order(text)
    tasks = [t for t in tasks
             if not t.get('pending') and not t.get('obsolete')]
    if not tasks:
        return ""
    lines = ['flowchart TD', '']
    lines.append('START([🚀 START])')
    node_ids = []
    for i, t in enumerate(tasks, 1):
        nid = f'T{i}'
        label = _sanitize_flow_label(t.get('label') or '')
        mark = '✅' if t.get('done') else '⬜'
        lines.append(f'{nid}["{mark} {label}"]')
        node_ids.append(nid)
    prev = 'START'
    for nid in node_ids:
        lines.append(f'{prev} --> {nid}')
        prev = nid
    lines.append(f'{prev} --> DONE([🏁 DONE])')
    return '\n'.join(lines) + '\n'


def generate_flow_file(project_dir=None):
    """(Re)generate flow.md from the project's task-order.md. Returns (ok, msg)."""
    d = Path(project_dir) if project_dir else _project_dir_for_flow()
    md = _flow_mermaid_from_task_order(d)
    if not md:
        return False, "No task-order.md in this project (nothing to diagram)."
    try:
        (d / 'flow.md').write_text(md, encoding='utf-8')
        n = len(md.splitlines()) - 2
        return True, f"Generated {d.name}/flow.md ({n} step(s))"
    except Exception as ex:
        return False, f"Generate failed: {ex}"


async def _generate_flow_click():
    """➕ Generate button: build flow.md from task-order.md, then render it."""
    ok, msg = generate_flow_file()
    ui.notify(msg, type='positive' if ok else 'warning')
    if ok:
        await render_flow_board()


def _open_flow_editor():
    """✏️ Edit button: edit the project's flow.md (Mermaid) + save re-renders."""
    d = _project_dir_for_flow()
    flow_path = d / 'flow.md'
    try:
        cur = flow_path.read_text(encoding='utf-8')
    except Exception:
        cur = ''
    with ui.dialog() as ed, ui.card().classes('w-[92vw] max-w-4xl h-[88vh] p-4 gap-2 dialog-drag fill-card'):
        with ui.row().classes('w-full items-center justify-between'):
            ui.label(f'✏️ flow.md — {d.name}').classes(
                'text-base font-bold text-emerald-400 drag-handle'
            )
            ui.button(icon='close', on_click=ed.close).props('flat dense color=gray size=sm').tooltip('Close')
        ui.label(
            'Mermaid flowchart syntax. Save re-renders the board.'
        ).classes('text-xs text-gray-400')
        ta = ui.textarea(value=cur).classes(
            'w-full flex-grow fill-textarea font-mono text-sm'
        )

        async def _save():
            try:
                flow_path.write_text(ta.value, encoding='utf-8')
                ui.notify('flow.md saved', type='positive')
                ed.close()
                await render_flow_board()
            except Exception as ex:
                ui.notify(f'Save failed: {ex}', type='negative')

        with ui.row().classes('gap-2'):
            ui.button('Save', on_click=_save).classes(
                'bg-emerald-600 text-white'
            )
            ui.button('Cancel', on_click=ed.close).props('flat')
    ed.open()





def _open_drawio_editor():
    """Open the self-hosted draw.io editor."""

    import json as _json
    import flow_graph as _fg
    import flow_drawio as _fd

    try:
        flow_dialog.close()
    except Exception:
        pass

    d = _project_dir_for_flow()

    try:
        graph = _fg.read_flow_graph(d)
        xml = _fd.graph_to_mxgraph_xml(graph)
    except Exception as ex:
        ui.notify(
            f'draw.io load failed: {ex}',
            type='negative'
        )
        return

    xml_js = _json.dumps(xml)

    # Store XML BEFORE creating/opening the dialog.
    ui.run_javascript(
        f'''
        window._drawioLoadXml = {xml_js};
        window._drawioXml = null;
        window._drawioIframeReady = false;

        console.log(
            '[DRAWIO] XML prepared:',
            window._drawioLoadXml.length
        );
        '''
    )

    with ui.dialog() as dlg:
        with ui.card().classes(
            'w-[95vw] max-w-7xl h-[88vh] '
            'p-2 gap-2 flex flex-col dialog-drag'
        ):

            with ui.row().classes(
                'w-full items-center gap-2 flex-shrink-0'
            ):
                ui.label(
                    f'🖊️ draw.io — {d.name}'
                ).classes(
                    'text-base font-bold text-orange-400'
                )

                ui.button(
                    '💾 Save to project',
                    on_click=lambda: _drawio_save(dlg, d)
                ).props('color=primary')

                ui.button(
                    'Close',
                    on_click=dlg.close
                ).props('flat')

            ui.html(
                '''
                <iframe
                    id="drawio-frame"
                    src="/drawio/index.html?embed=1&proto=json&spin=1&modified=unsavedChanges&saveAndExit=0&noSaveBtn=0"
                    style="
                        width:100%;
                        height:100%;
                        min-height:0;
                        flex:1 1 auto;
                        border:0;
                        display:block;
                        background:#fff;
                    "
                    onload="
                        console.log('[DRAWIO] iframe onload');
                        window._drawioIframeReady = true;
                    "
                ></iframe>
                ''',
                sanitize=False
            ).classes(
                'w-full flex-1 min-h-0 overflow-hidden'
            )

    dlg.open()

    # Give Vue/NiceGUI time to mount the dialog.
    ui.timer(
        1.0,
        lambda: ui.run_javascript(
            '''
            (function() {

                console.log('[DRAWIO] checking iframe...');

                var f = document.getElementById('drawio-frame');

                console.log('[DRAWIO] iframe:', f);
                console.log(
                    '[DRAWIO] ready:',
                    window._drawioIframeReady
                );

                if (!f) {
                    console.error(
                        '[DRAWIO] iframe STILL NOT FOUND'
                    );
                    return;
                }

                console.log(
                    '[DRAWIO] iframe found successfully'
                );

                function sendLoad() {

                    if (
                        f.contentWindow &&
                        window._drawioLoadXml
                    ) {

                        console.log(
                            '[DRAWIO] sending load'
                        );

                        f.contentWindow.postMessage(
                            JSON.stringify({
                                action: 'load',
                                xml: window._drawioLoadXml,
                                autosave: 1
                            }),
                            '*'
                        );
                    }
                }

                sendLoad();

                setTimeout(sendLoad, 1000);
                setTimeout(sendLoad, 3000);

            })();
            '''
        ),
        once=True
    )





async def _drawio_save(dlg, project_dir):
    """Read the diagram back from draw.io and persist it as flow_graph.json + flow.md."""
    import flow_drawio as _fd
    import flow_graph as _fg

    try:
        xml = await ui.run_javascript(
            '''
            (function() {
                return window._drawioXml || "";
            })()
            '''
        )
    except Exception as ex:
        ui.notify(
            f'Could not read diagram from draw.io: {ex}',
            type='negative'
        )
        return

    if not xml:
        ui.notify(
            'Nothing to save — draw.io has not returned a diagram yet.',
            type='warning'
        )
        return

    try:
        graph = _fd.mxgraph_xml_to_graph(xml)

        ok, msg = _fg.write_flow_graph(project_dir, graph)

        ui.notify(
            msg,
            type='positive' if ok else 'negative'
        )

        if ok:
            dlg.close()
            await render_flow_board()

    except Exception as ex:
        ui.notify(
            f'draw.io save failed: {ex}',
            type='negative'
        )



def save_flow(content: str, project_dir: Optional[str] = None) -> Path:
    """Create/update the project's flow.md."""
    base = Path(project_dir or app_state.get("workdir") or BASE_DIR)
    base.mkdir(parents=True, exist_ok=True)

    path = base / "flow.md"
    path.write_text(content, encoding="utf-8")

    return path

async def restart_panel():
    """Re-exec this panel in place (it runs bare from a terminal, no unit)."""
    ui.notify('Restarting panel — reconnect in a few seconds...')
    await asyncio.sleep(0.5)
    os.execv(sys.executable, [sys.executable, str(BASE_DIR / 'aichat_xterm.py')])

# ============================================================
# CHAT HISTORY (scrollable conversation)
# ============================================================

chat_messages = []  # list[{'role': 'user'|'assistant', 'text': str}]


# ============================================================
# VIKING SEMANTIC MEMORY
# ============================================================


# Doc processing lives in aichat_docs.py (keeps this file slim).

def background_task(current_client):
  # Perform heavy background work (e.g., PDF parsing, LLM call)
  success = True
  msg = "Processed successfully!"

  # Safely check if the user is still connected before touching UI elements
  if current_client.has_socket_connection:
    with current_client:
      ui.notify(
          msg, type="positive" if success else "negative", timeout=10000
      )

def _fmt_dur(sec: float) -> str:
    """Short elapsed stamp: 4.2s / 3m 12s."""
    try:
        if sec < 60:
            return f"{sec:.1f}s"
        m = int(sec // 60)
        return f"{m}m {sec - m * 60:.0f}s"
    except Exception:
        return "?"


# Doc resolution + page lookup live in aichat_docs.py (keeps this file slim).


def viking_find(query: str, tokens: int = 700) -> str:
    """Retrieve relevant semantic memory without breaking the chat."""
    try:
        from aichat_project_controller import vfind
        return vfind(query, tokens) or ""
    except Exception as ex:
        logging.warning("Viking retrieval failed: %s", ex)
        return ""


def viking_put(path: str, data: str) -> bool:
    """Store a small memory without breaking the chat."""
    try:
        from aichat_project_controller import vput
        vput(path, data)
        return True
    except Exception as ex:
        logging.warning("Viking store failed: %s", ex)
        return False


def _workspace_projects() -> list:
    """List real project folder names under WORKSPACE. Never raises."""
    workspace = BASE_DIR / 'WORKSPACE'
    try:
        return sorted(
            p.name for p in workspace.iterdir()
            if p.is_dir() and not p.name.startswith(('.', '_'))
        )
    except Exception:
        return []


# Words/phrases that signal the user is referring to PAST work (so we may
# want semantic memory). General/meta questions skip Viking entirely.
_RECALL_WORDS = (
    'remember', 'memory', 'memory:', 'recall', 'previously', 'earlier',
    'last time', 'the other day', 'what did we', 'we did', 'we talked',
    'we discussed', 'our plan', 'the plan we', 'past work', 'looked at',
    'worked on', 'our project', 'the project we',
)


def _should_pull_viking(prompt: str, project_names: list) -> bool:
    # Only explicit recall language pulls semantic memory. Project-name
    # references are NOT a trigger — get_project_context() already injects
    # the authoritative task-order/run-state for any project on disk, and
    # pulling Viking for those was matching stale memories ("Harmony AI test
    # memory") and poisoning the answer.
    p = (prompt or '').lower()
    return any(w in p for w in _RECALL_WORDS)


def get_viking_context(prompt: str, project_names: list) -> str:
    """Retrieve semantic memory ONLY when the request references past work.

    General knowledge ("explain TBI"), meta questions ("what role are we on")
    and greetings never pull Viking memory — that's what caused the Ghost AI /
    Pac-Man hallucinations. When it IS relevant, add the best matches as
    background.
    """
    if not _should_pull_viking(prompt, project_names):
        return ""

    context = viking_find(prompt, 700)
    if not context:
        return ""

    return (
        "PAST-PROJECT MEMORY (relevant snippets — use only if it helps):\n"
        f"{context}"
    )


_HANDOFF_STOP = frozenset(
    'what when where which who whom whose how why not dont doesn isn arent '
    'wasn weren have has had will would could should shall there their they '
    'them then than that this these those with from into over under again '
    'very just about also please thank thanks hello'.split())

_HANDOFF_LANES = ('plan', 'exec', 'qc', 'brainstorm')


def get_lane_handoff(prompt: str) -> str:
    """Previous-lane context for plan/exec/qc/brainstorm: last chat exchange sharing a
    topic keyword with the new prompt. Empty when nothing relevant (the lane's
    clarification rule then covers the ambiguity). Never raises."""
    try:
        if app_state.get('role') not in _HANDOFF_LANES:
            return ''
        words = {w for w in re.findall(r'[a-z0-9]{5,}', (prompt or '').lower())
                 if w not in _HANDOFF_STOP}
        if not words:
            return ''
        msgs = [m for m in chat_messages
                if isinstance(m, dict) and m.get('text')]
        for i in range(len(msgs) - 1, -1, -1):
            m = msgs[i]
            if m.get('role') != 'assistant' or len(m.get('text', '')) < 60:
                continue
            low = m['text'].lower()
            if not any(w in low for w in words):
                continue
            prev = msgs[i - 1] if i > 0 and msgs[i - 1].get('role') == 'user' else None
            block = ''
            if prev:
                block += f"User asked: {prev['text'][:600]}\n"
            block += f"Previous lane answered: {m['text'][:1200]}"
            lane = {  # which lane wrote it (label like 'AI Harmony B')
                'B': 'brainstorm', 'P': 'plan', 'E': 'exec', 'QC': 'qc',
            }.get((m.get('label') or '').rsplit(' ', 1)[-1], 'previous lane')
            return f"[FROM {lane} — last on-topic exchange]:\n{block}"
        return ''
    except Exception:
        return ''


_TRIVIAL_FIRST = frozenset(
    'hello hi hey yo hiya sup howdy hola ola olá oi thanks thank ok okay cool '
    'bye goodbye'.split())

_TRIVIAL_PHRASES = (
    'how are you', 'how are u', 'how are you doing', 'whats up', 'what is up',
    'hows it going', 'good morning', 'good afternoon', 'good evening',
    'good night', 'goodnight', 'good morning my friend',
)


def _is_trivial(prompt: str) -> bool:
    """A greeting or bare acknowledgment that needs NO context. These must not
    get the root/here/project dumps — a 1.2B model regurgitates a huge folder
    listing + role hats for 'hello' instead of answering (47s nonsense)."""
    p = re.sub(r'[^\w\s\']', ' ', (prompt or '')).strip().lower()
    p = re.sub(r'\s+', ' ', p)
    if p in _TRIVIAL_PHRASES:
        return True
    words = p.split()
    if words and words[0] in _TRIVIAL_FIRST and len(words) <= 3:
        return True
    return False


def build_model_prompt(prompt: str) -> str:
    """Combine orientation + project structure + task status + handoff +
    selective memory with the request. Pure general-knowledge questions go
    out bare (role/hat + topic pin only) via the gate below."""
    parts = []

    # If we ran an instant terminal command, feed its output to the model first.
    # Capped — a `!cat hugefile` would otherwise blow past a small model's
    # context window ("Exceed max_input_tokens limit").
    has_terminal = bool(app_state.get("last_context"))
    if has_terminal:
        _lc = str(app_state["last_context"])[:3000]
        parts.append(f"[RECENT TERMINAL OUTPUT]:\n{_lc}\n")
        app_state["last_context"] = None  # Clear it after using once

    trivial = _is_trivial(prompt)

    project_context = "" if trivial else get_project_context(prompt)
    if project_context:
        parts.append(project_context)

    handoff = "" if trivial else get_lane_handoff(prompt)
    if handoff:
        parts.append(handoff)

    projects = _workspace_projects()
    context = "" if trivial else get_viking_context(prompt, projects)
    if context:
        parts.append(context)

    # GENERAL-KNOWLEDGE GATE (selection lens): no lens caught anything, so the
    # request is about the world, not the workspace. Skip the ALWAYS-ON
    # orientation below — ~700 chars of folder/project framing is what tips a
    # 3B model into answering about the codebase instead of the question.
    # Orientation questions keep working: they carry listing/deixis words, so
    # _references_project() catches them and this gate never fires for them.
    general = (not trivial and not has_terminal
               and not project_context and not handoff and not context)

    # ALWAYS-ON orientation: cheap (~8 lines) so the model never gets lost,
    # no matter how the question is phrased. This is what fixes "do you know
    # where the project folders are?" — that phrasing misses every trigger
    # list, so the gated project block below is empty, but the root index
    # is still there. SKIPPED for greetings — a 1.2B model echoes the folder
    # dump back instead of answering "hello". ALSO skipped in
    # general-knowledge mode (gate above).
    root_index = "" if (trivial or general) else build_root_index(str(BASE_DIR))
    if root_index:
        parts.append(root_index)

    # Current folder (the tree click sets this). Always on so "at this
    # level" / "here" questions answer from where the user IS, instead of
    # defaulting to [PROJECTS] every time. Also skipped for greetings and
    # general-knowledge questions.
    here_index = "" if (trivial or general) else build_here_index(app_state.get("workdir"))
    if here_index:
        parts.append(here_index)

    state = "" if trivial else _state_line(prompt)
    if state:
        if general:
            parts.append(
                state + "\n(TOPIC PIN: answer ONLY the CURRENT REQUEST below; "
                "any block above is orientation, not the topic. "
                "Do not change topic.)")
        else:
            parts.append(state)

    logging.info(
        "prompt ctx chars: root=%d here=%d proj=%d viking=%d state=%d req=%d",
        len(root_index), len(here_index), len(project_context),
        len(context), len(state), len(prompt))
    app_state["last_ctx"] = {
        'root': len(root_index), 'here': len(here_index),
        'proj': len(project_context), 'viking': len(context),
        'state': len(state), 'req': len(prompt),
    }

    if not parts:
        return prompt

    return "\n\n".join(parts) + f"\n\n--- CURRENT REQUEST ---\n{prompt}"


# ============================================================
# BRAINSTORM "HATS" (state, not roles)
# ============================================================

# A small finite vocabulary so a small model isn't inferring from a huge
# rulebook. The app detects intent and redeclares the hat each turn.
_HAT_KEYWORDS = {
    'expert': ('expert', 'subject matter expert', 'full detail', 'exhaustive',
               'deep dive', 'dive deep', 'tell me everything',
               'everything you know', 'maximum detail',
               'as detailed as possible', 'expand more', 'go deeper',
               'tell me more', 'elaborate', 'in depth', 'in-depth',
               'every step', 'top to bottom', 'sme'),
    'explain': ('explain', 'give me more detail', 'more detail', 'articulate',
                'walk me through', 'break down', 'unpack', 'expand'),
    'clarify': ('what do you mean', 'clarify', 'i am confused', 'i don\'t get',
                'don\'t understand', 'what does that mean'),
    'structure': ('break into steps', 'structure it', 'plan it out', 'outline',
                  'step by step', 'steps', 'organize', 'lay out'),
    'summarize': ('summarize', 'summarise', 'summary', 'condense', 'shorten',
                  'tldr', 'tl;dr', 'in short', 'brief', 'make it shorter',
                  'give me the gist', 'boil down'),
}

_DEFAULT_HAT = 'EXPLORE'

# Triggers that ask for an EVEN SHORTER summary than plain "summarize".
# When any of these hits on a summarize request, the state line sets a
# stricter TARGET so the model compresses further instead of guessing.
_TIGHT_SUMMARY_WORDS = (
    'even more', 'even shorter', 'very few words', 'very short', 'fewer words',
    'few words', 'less words', 'a few words', 'short summary', 'diminutive',
    'tiny', 'ultra short', 'ultrashort', 'super short', 'one line',
    'one sentence', 'briefly', 'really short', 'as short as',
)

# Expand-direction intensity (mirror of summarize). Plain "explain/expand" is
# idea-level; "expand more / even more" asks for a structured breakdown; the
# technical/exhaustive phrasings ask for concrete minutiae (exec-level detail).
_EXPAND_STRUCTURED_WORDS = (
    'expand more', 'even more', 'go deeper', 'more detail', 'in more depth',
    'explain more', 'break it down', 'break down', 'walk me through',
    'section by section', 'subsection', 'elaborate', 'explain that more',
)
_EXPAND_TECHNICAL_WORDS = (
    'articulate better', 'technical detail', 'technically', 'in detail',
    'exhaustive', 'minutiae', 'fully explain', 'explain everything',
    'every detail', 'very detailed', 'in-depth', 'in depth', 'nitty gritty',
)


def detect_hat(prompt: str) -> str:
    """Return the current brainstorm hat for a user prompt (EXPLORE default).

    Deliberately dumb keyword matching, NOT a sentence classifier — that's the
    whole point: a finite vocabulary of states the small model can follow.
    """
    p = (prompt or '').lower()
    for hat, words in _HAT_KEYWORDS.items():
        if any(w in p for w in words):
            return hat.upper()
    return _DEFAULT_HAT


def _summarize_target(prompt: str) -> str:
    """Return the shorten-intensity target for a summarize request.

    'summarize' alone -> SHORT PARAGRAPH. Any tight trigger -> ONE SENTENCE.
    """
    p = (prompt or '').lower()
    if any(w in p for w in _TIGHT_SUMMARY_WORDS):
        return 'ONE SENTENCE (or up to 3 short bullets)'
    return 'SHORT PARAGRAPH'


def _explain_depth(prompt: str) -> str:
    """Return the expand-intensity depth for an explain request.

    Plain explain/expand -> IDEA (conceptual subtopics).
    structured triggers  -> STRUCTURED (planner-level breakdown).
    technical triggers   -> TECHNICAL (exec-level concrete detail).
    """
    p = (prompt or '').lower()
    if any(w in p for w in _EXPAND_TECHNICAL_WORDS):
        return 'TECHNICAL (concrete, precise detail)'
    if any(w in p for w in _EXPAND_STRUCTURED_WORDS):
        return 'STRUCTURED (ordered breakdown, subsections)'
    return 'IDEA (2-5 conceptual subtopics)'


def _state_line(prompt: str) -> str:
    """The injected ROLE/HAT state block the model sees."""
    hat = detect_hat(prompt)
    app_state["hat"] = hat  # record for UI + debug
    lines = [
        f"ROLE: {app_state.get('role', 'brainstorm').upper()}",
        f"HAT: {hat}",
    ]
    if hat == 'SUMMARIZE':
        lines.append(f"LENGTH: {_summarize_target(prompt)}")
    elif hat == 'EXPERT':
        lines.append(
            "MODE: EXPERT — full subject-matter-expert breakdown: every step of "
            "the process (goal → plan → build → QC), spare no detail."
        )
    elif hat == 'EXPLAIN':
        lines.append(f"DEPTH: {_explain_depth(prompt)}")
    elif hat == 'EXPLORE':
        lines.append(
            "MODE: Terse — 1-4 lines. Do NOT produce a plan or expert breakdown; "
            "the user will say 'expand' or 'tell me more' for the full version."
        )
    lines.append(
        "(Stay in this role; the hat may switch each turn without changing role.)"
    )
    return '\n'.join(lines)


# ============================================================
# PROJECT CONTEXT (structure + task-order status)
# ============================================================

# Words that mean the user is asking about the current (selected) project, even
# without naming it. Only then do we resolve the selected folder as context.
_DEIXIS_WORDS = (
    'this project', 'my project', 'the project', 'our project', 'the game',
    'my game', 'this app', 'my app', 'the app', 'the folder', 'this folder',
    'current project', 'current folder', 'task order', 'task-order',
    'milestone', 'the tasks', 'my tasks', 'the files', 'my files',
    'the code', 'my code',
)

# Task-status / count language — "how many unchecked", "any outstanding", etc.
# These are DATA lookups into the task-order, not conversation recall.
_TASK_STATUS_WORDS = (
    'how many', 'unchecked', 'not checked', 'not done', 'outstanding',
    'pending', 'left to do', 'remaining', 'count', 'how much', 'status of',
    'what is done', 'whats done', 'what is left', 'completed', 'incomplete',
    'task-order', 'task order', 'task.md', 'checklist', 'how far along',
    'unfinished', 'any tasks', 'tasks left', 'anything left', 'anything pending',
    'done yet', 'finished yet', 'whats left', 'what is left to', 'still open',
    'still need', 'left on', 'tasks', 'milestones', 'steps', 'unchecked',
    'todo', 'to-do', 'to do', 'to-do list',
)

# Explicit lookup / listing language — "do you see X", "is there a file named Y",
# "list the projects". These are FILESYSTEM lookups, never conversation recall.
_LISTING_WORDS = (
    'do you see', 'do we have', 'is there a', 'are there any', 'any project',
    'any folder', 'any file', 'list the projects', 'list projects',
    'list the files', 'list files', 'what files', 'what projects',
    'what folders', 'show me the files', 'show me the projects', 'called',
    'named', 'find the file', 'find the project', 'find file', 'find project',
    # name / pattern conditions
    'starts with', 'starts by', 'startsby', 'begins with', 'starting with',
    'start with', 'ends with', 'ends by', 'ending with', 'ending in',
    'end with', 'end in', 'contains', 'has the number', 'numbered',
    'in the name', 'matches', 'like', 'extension', 'ends in', 'file type',
    'matching',
    # time / age conditions (group by creation or modification date)
    'younger than', 'older than', 'newer than', 'created after', 'created before',
    'modified after', 'modified before', 'creation date', 'created on',
    'modified on', 'last modified', 'how old', 'recently', 'since',
    'in the last', 'date', 'timestamp', 'age of',
    # location / orientation language — "where are the project folders",
    # "where is X", "location of", root questions. These missed every list
    # before, so orientation questions got zero context.
    'where are', 'where is', 'location of', 'project folders', 'project folder',
    'main folder', 'root folder', 'app home', 'which folder', 'what folder',
    # current-level language — "how many folders at this level" must resolve
    # to the working folder, never to [PROJECTS].
    'at this level', 'this level', 'at this folder', 'in this folder',
    'in here', 'right here', 'at the top level',
)


def _references_project(prompt: str) -> Optional[str]:
    """Return the project name the prompt is about, or None if it isn't about
    any project (e.g. a general topic like quantum physics).

    Two cheap signals only (name match + deixis), NOT topic classification:
      - the prompt names a real project folder -> that folder
      - the prompt uses deictic language ("the game", "my project") -> the
        SELECTED folder (which may be None on a neutral boot)
      - the prompt uses listing language ("do you see X", "is there a file
        named Y"), even without a name -> the selected folder (a filesystem
        lookup, not conversation recall)
    Otherwise None -> no project context is injected, so off-topic chat stays
    clean and can't leak a game's task-order into a physics discussion.
    """
    p = (prompt or '').lower()
    for name in _workspace_projects():
        if name.lower() in p:
            return name
    if any(w in p for w in _DEIXIS_WORDS) or any(w in p for w in _LISTING_WORDS) \
            or any(w in p for w in _TASK_STATUS_WORDS):
        return app_state.get("active_project")
    # Doc-action verbs ("summarize cheatsheet", "explain ideas") name a FILE,
    # not a project — but they still need the active project's file list,
    # otherwise the model answers with zero context and invents "no files".
    if any(v in p for v in ('summar', 'explain', 'describe', 'read this',
                            'extract', 'attach')):
        return app_state.get("active_project")
    return None


def get_project_context(prompt: str) -> str:
    """Inject the deterministic project/workspace block ONLY when the prompt is
    actually about a project. Neutral at boot — never auto-reads
    active_project.txt — so a general chat has no stale project context."""
    workspace = BASE_DIR / 'WORKSPACE'
    if not workspace.is_dir():
        return ""

    project = _references_project(prompt)

    try:
        return build_project_context(project, str(workspace))
    except Exception as e:
        logging.warning("project context failed: %s", e)
        return ""


# ============================================================
# MODEL OUTPUT CLEANUP
# ============================================================

def clean_model_output(text: str) -> str:
    text = re.sub(r'<\|[^<>]*?\|>', '', text)
    text = re.sub(r'<\|[^<>\s]*', '', text)
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = re.sub(r'<thinking>.*?</thinking>', '', text, flags=re.DOTALL)
    # Granite-style plain-text thinking (no tags): "Thinking...\n...\n...done thinking."
    text = re.sub(r'Thinking\.\.\s*.*?\.\.\.done thinking\.\s*', '', text,
                  flags=re.DOTALL | re.IGNORECASE)
    return text.strip()


# ============================================================
# FILE TREE
# ============================================================

def get_tree_data(path: Path, depth: int = 0, max_depth: int = 3):
    """
    Build a small filesystem tree.

    Recurses deep enough to reach a project's files (WORKSPACE/<project>/
    task-order.md) so the tree is actually navigable — but skips the heavy
    dirs (venv_ui, Models, blobs, snapshots) so startup stays fast.
    """
    if depth > max_depth:
        return []

    children = []

    try:
        entries = sorted(
            path.iterdir(),
            key=lambda p: (not p.is_dir(), p.name.lower())
        )

        for item in entries:
            # Step 1: Skip hidden files and bloated/unnecessary system folders.
            # Keep Models/ visible (it holds the GGUFs) — only its blob/cache
            # subdirs are heavy, and those are skipped by name below.
            if item.name.startswith('.') or item.name in (
                'node_modules', '__pycache__', '.git', 'venv', '.venv',
                'venv_ui', 'blobs', 'manifests', 'snapshots',
            ):
                continue

            if item.is_dir():
                children.append({
                    'id': str(item),
                    'label': item.name,
                    'is_folder': True,
                    'children': get_tree_data(
                        item,
                        depth + 1,
                        max_depth
                    ),
                })
            else:
                children.append({
                    'id': str(item),
                    'label': item.name,
                    'is_folder': False,
                })

    except (PermissionError, OSError) as ex:
        logging.warning("Could not read %s: %s", path, ex)

    return children

# ============================================================
# MAIN LAYOUT
# ============================================================

with ui.column().classes('w-full h-screen bg-gray-900 text-gray-100 p-4'):
    
    # Define tree panel & toggle logic early so header can access it safely in scope
    tree_panel = ui.column().classes(
        'absolute left-0 top-0 z-50 '
        'w-80 h-full '
        'bg-gray-800/80 p-3 rounded-lg '
        'border border-gray-700 shadow-2xl '
        'overflow-y-auto'
    )




    tree_panel.visible = False

    def toggle_tree():
        tree_panel.visible = not tree_panel.visible
        tree_panel.update()

    # Capture phase lets this work even when xterm.js has focus.
    # Shortcuts: legacy Shift+M, plus Ctrl+Home and F11 (preventDefault stops
    # the browser fullscreen on F11).
    ui.timer(0.1, lambda: ui.run_javascript('''
        document.addEventListener('keydown', function(e) {
            if ((e.shiftKey && e.key === '+')
                || (e.ctrlKey && e.key === 'Home')
                || (e.key === 'F11')) {
                e.preventDefault();
                e.stopPropagation();

                const button = document.querySelector('.menu-toggle-btn');
                if (button) {
                    button.click();
                }
            }
        }, true);
    '''), once=True)



    
    def toggle_info():
        try:
            info_panel.visible = not info_panel.visible
            info_panel.update()
        except Exception as ex:
            ui.notify(f'Info panel: {ex}', type='warning')

    # --------------------------------------------------------
    # HEADER
    # --------------------------------------------------------

    with ui.row().classes(
        'w-full justify-between items-center mb-2 px-1'
    ):
        with ui.row().classes('items-center gap-2'):

            ui.button(
                icon='menu',
                on_click=toggle_tree
            ).props(
                'flat dense color=emerald-400'
            ).classes(
                'menu-toggle-btn'
            )

            ui.label(
                'Harmony AI Terminal Interface'
            ).classes(
                'text-xl font-bold tracking-wide text-emerald-400'
            )

            model_status_lbl = ui.label(
                '⚙️ …'
            ).classes(
                'text-xs font-mono text-gray-400 ml-1'
            )

        ui.label(
            'Press [Ctrl+Home / F11] to toggle file tree'
        ).classes(
            'text-xs text-gray-400'
        )

        ui.button(
            icon='refresh',
            on_click=restart_panel
        ).props(
            'flat dense color=orange'
        ).tooltip('Restart this panel')

        ui.button(
            icon='tune',
            on_click=lambda: toggle_info()
        ).props(
            'flat dense color=sky'
        ).tooltip('Toggle session info column')

    def _refresh_model_status():
        """Poll Ollama /api/ps and update the tiny header line. Auto-reflects
        a model loaded manually via `ollama run` — no reload needed."""
        try:
            lm = app_state.get('loading_model')
            if lm:
                model_status_lbl.text = f'⏳ loading {lm}...'
                model_status_lbl.classes(
                    remove='text-emerald-300 text-gray-400',
                    add='text-yellow-300')
                return
            loaded = _mc.loaded_names() if HAS_MC else []
            if loaded:
                model_status_lbl.text = f'⚙️ {loaded[0]}'
                model_status_lbl.classes(
                    remove='text-yellow-300 text-gray-400',
                    add='text-emerald-300')
            else:
                model_status_lbl.text = '⚙️ (no model)'
                model_status_lbl.classes(
                    remove='text-emerald-300 text-yellow-300',
                    add='text-gray-400')
        except Exception:
            pass

    ui.timer(2.0, _refresh_model_status)
    _refresh_model_status()

    # Auto-persist working state (role/hat/project/workdir) every 10s so a
    # restart comes back "as it was".
    ui.timer(10.0, save_settings)

    # Auto-warm the configured model in a BACKGROUND THREAD shortly after boot,
    # so the FIRST chat turn doesn't pay a ~60-90s cold load (Ollama reading the
    # GGUF from disk). This is the whole "big initial lag vs terminal" — the
    # terminal stays warm because you `ollama run` first; the panel used to wait
    # for the first message to load it.
    def _auto_warm_default():
        try:
            import threading as _th
            import aichat_model_controller as _mcw
            _m = None
            _cfg = Path.home() / '.config' / 'aichat' / 'config.yaml'
            for _ln in _cfg.read_text().splitlines():
                if re.match(r'^\s*model\s*:', _ln):
                    _m = _ln.split(':', 1)[1].strip()
                    break
            if _m:
                _m = re.sub(r'^ollama:', '', _m).strip()
                _th.Thread(
                    target=lambda: _mcw.load_model(_m, timeout=900, base_dir=str(BASE_DIR)),
                    daemon=True).start()
        except Exception as _e:
            logging.warning('auto-warm failed: %s', _e)

    ui.timer(4.0, lambda: _auto_warm_default(), once=True)

    # --------------------------------------------------------
    # MAIN SPLIT WORKSPACE
    # --------------------------------------------------------

    with ui.row().classes(
        'w-full flex-grow overflow-hidden min-h-0'
    ):



        # ====================================================
        # FILE TREE PANEL
        # ====================================================

        with tree_panel:

            with ui.row().classes(
                'w-full justify-between items-center mb-2'
            ):
                ui.label(
                    'Workspace Navigation'
                ).classes(
                    'text-bold text-base text-emerald-400'
                )

                ui.button(
                    icon='close',
                    on_click=toggle_tree
                ).props(
                    'flat dense color=gray size=sm'
                )

            ui.label(
                'Click a file to attach context, or folder '
                'to change working environment'
            ).classes(
                'text-xs text-gray-400 mb-2'
            )

            selected_file_label = ui.label(
                'No file selected'
            ).classes(
                'text-xs text-yellow-400 mb-1 italic'
            )

            workdir_label = ui.label(
                f'Current Project Path: {Path(app_state.get("workdir") or BASE_DIR).name}'
            ).classes(
                'text-xs text-emerald-400 mb-2 font-mono'
            )

            # ----------------------------------------------
            # TREE CLICK
            # ----------------------------------------------

            def handle_click(e):
                if not e.value:
                    return

                path_str = e.value
                path = Path(path_str)

                if path.is_file():
                    if path.name in ('flow.json', 'flow.md'):
                        # Clicking the flow file opens the visual board — it does
                        # NOT attach the JSON as chat context (that's what dumped
                        # raw JSON into answers). Target that file's project.
                        _ws = BASE_DIR / 'WORKSPACE'
                        if _ws in path.parents:
                            app_state["active_project"] = path.parent.name
                        app_state["workdir"] = str(path.parent)
                        toggle_tree()
                        flow_dialog.open()
                        ui.timer(0.2, render_flow_board, once=True)
                    else:
                        app_state["selected_file"] = path_str
                        selected_file_label.text = f'Context: {path.name}'
                        ui.notify(f'Attached File: {path.name}')
                        toggle_tree()

                elif path.is_dir():
                    app_state["workdir"] = path_str
                    workdir_label.text = f'Current Project Path: {path.name}'
                    ui.notify(f'Current Project Path: {path.name}')
                    # Track selected project folder so prompts know the
                    # project context (task-order.md lives here).
                    ws = BASE_DIR / 'WORKSPACE'
                    if ws in path.parents or path == ws:
                        app_state["active_project"] = path.name
                    else:
                        app_state["active_project"] = None
                    try:
                        save_settings()
                    except Exception:
                        pass

                    # Dynamically update tree root to the newly selected folder
                    tree.data = [{
                        'id': str(path),
                        'label': path.name,
                        'is_folder': True,
                        'children': get_tree_data(path),
                    }]
                    tree.update()

            # ----------------------------------------------
            # BUILD TREE ONCE
            # ----------------------------------------------

            tree_data = [{
                'id': str(BASE_DIR),
                'label': 'AI_ENGINE',
                'is_folder': True,
                'children': get_tree_data(BASE_DIR),
            }]

            with ui.scroll_area().classes(
                'w-full flex-grow bg-transparent'
            ):

                tree = ui.tree(
                    tree_data,
                    label_key='label',
                    on_select=handle_click
                ).classes(
                    'w-full bg-transparent text-white q-pa-sm'
                )

                tree.add_slot(
                    'default-header',
                    r'''
                    <span
                        :class="props.node.is_folder
                            ? 'text-emerald-400 font-bold'
                            : 'text-gray-300'"
                    >
                        {{ props.node.label }}
                    </span>
                    '''
                )

            # Bottom close — matches the top one so the panel can be dismissed
            # from either end.
            with ui.row().classes('w-full justify-end mt-2'):
                ui.button(
                    'Close tree',
                    icon='close',
                    on_click=toggle_tree
                ).props(
                    'flat dense color=gray'
                ).classes('text-xs')

        # ====================================================
        # MAIN CHAT / EXECUTION PANEL
        # ====================================================

        with ui.column().classes(
            'relative flex-grow min-w-0 min-h-0 h-full '
            'flex flex-col gap-3 '
            'bg-gray-800 p-4 rounded-lg '
            'border border-gray-700 shadow-xl overflow-hidden'
        ):

            # ------------------------------------------------
            # CONTROLS
            # ------------------------------------------------

            def _edit_dialog(target):
                try:
                    _cur = read_text_capped(target, 100000)
                except Exception:
                    _cur = ""
                _twin = str(target)
                if _twin.startswith('/home/joao/STORAGE/'):
                    _twin = _twin.replace('/home/joao/STORAGE/',
                                          '/home/joao/REMOTE_HOME/STORAGE/', 1)
                with ui.dialog() as _dlg, ui.card().classes('w-[92vw] max-w-6xl h-[88vh] p-4 gap-2 dialog-drag fill-card'):
                    with ui.row().classes('w-full items-center justify-between'):
                        ui.label(f'✏️ {Path(target).name}').classes('text-base font-bold text-sky-300 drag-handle')
                        ui.button(icon='close', on_click=_dlg.close).props('flat dense color=gray size=sm').tooltip('Close')
                    ui.input(value=_twin).props('readonly dense').classes('w-full text-xs').tooltip('Your local copy — open in your own editor')
                    with ui.row().classes('w-full no-wrap gap-2 flex-grow min-h-0'):
                        with ui.column().classes('w-1/2 h-full min-h-0'):
                            ui.label('Markdown (edit + Save)').classes(
                                'text-xs font-semibold text-gray-400'
                            )
                            _ed = ui.textarea(value=_cur).classes(
                                'w-full flex-grow fill-textarea font-mono text-sm'
                            )
                        with ui.column().classes('w-1/2 h-full min-h-0 overflow-auto'):
                            ui.label('Preview (live)').classes(
                                'text-xs font-semibold text-gray-400'
                            )
                            ui.markdown().bind_content_from(_ed, 'value')
                    def _save():
                        try:
                            Path(target).write_text(_ed.value, encoding='utf-8')
                            ui.notify(f'Saved {Path(target).name}', type='positive')
                            _dlg.close()
                        except Exception as _ex:
                            ui.notify(f'Save failed: {_ex}', type='negative')
                    with ui.row().classes('gap-2'):
                        ui.button('Save', on_click=_save).props('color=primary')
                        ui.button('Cancel', on_click=_dlg.close).props('flat')
                _dlg.open()

            def _open_in_editor(e):
                if not e.value:
                    return
                try:
                    edit_checkbox.set_value(False)
                except Exception:
                    pass
                target = (app_state.get("selected_file")
                          or app_state.get("last_doc"))
                if not target:
                    ui.notify('Nothing to edit — select a file or run !pdf first',
                              type='warning')
                    return
                _edit_dialog(target)

            with ui.row().classes(
                'gap-2 items-center'
            ):

                ui.label('Role:').classes(
                    'text-xs font-semibold text-gray-400'
                )

                role_buttons = {}

                roles = [
                    'brainstorm',
                    'plan',
                    'exec',
                    'qc',
                ]

                def set_role(role, notify=True):
                    app_state["role"] = role
                    try:
                        save_settings()
                    except Exception:
                        pass

                    for name, button in role_buttons.items():
                        button.style(
                            f"{_ROLE_BTN_BG[name]};{_BTN_TEXT};"
                            f"{_ACTIVE_FX if name == role else _INACTIVE_FX}"
                        )

                    if notify:
                        ui.notify(
                            f'Role switched to: {role.upper()}'
                        )

                for role in roles:

                    button = ui.button(
                        role.upper(),
                        on_click=lambda r=role: set_role(r)
                    ).classes(
                        'text-xs py-1 px-3'
                    ).style(f"{_ROLE_BTN_BG[role]};{_BTN_TEXT};{_INACTIVE_FX}")

                    role_buttons[role] = button

                set_role('brainstorm', notify=False)

                wrap_checkbox = ui.checkbox(
                    'Wrap output',
                    value=True,
                ).props(
                    'dark'
                ).classes(
                    'text-xs text-emerald-400 font-semibold ml-2'
                )
                debug_checkbox = ui.checkbox(
                    'Debug Detail'
                ).props(
                    'dark'
                ).classes(
                    'text-xs text-yellow-400 font-semibold ml-2'
                )
                edit_checkbox = ui.checkbox(
                    '✏️ Edit file',
                    value=False,
                    on_change=_open_in_editor,
                ).props(
                    'dark'
                ).classes(
                    'text-xs text-sky-300 font-semibold ml-2'
                ).tooltip('Edit selected / last doc (editor + preview)')

            hat_label = ui.label().classes(
                'text-xs font-mono font-semibold ml-2'
            )
            # Define helper first
            def _hat_color(hat):
                return {
                    'EXPLORE': 'text-sky-300',
                    'CLARIFY': 'text-amber-300',
                    'EXPLAIN': 'text-emerald-300',
                    'STRUCTURE': 'text-purple-300',
                    'SUMMARIZE': 'text-pink-300',
                }.get(hat, 'text-gray-300')

            hat_label = ui.label().classes(
                'text-xs font-mono font-semibold ml-2'
            )

            # Then define and call the refresh function
            def _refresh_hat_label():
                r = app_state.get('role', 'brainstorm').upper()
                h = app_state.get('hat', 'EXPLORE')
                hat_label.set_text(f'· {r} / {h}')
                hat_label.classes(
                    remove='text-sky-300 text-amber-300 text-emerald-300 '
                           'text-purple-300 text-pink-300 text-gray-300',
                    add=_hat_color(h)
                )

            _refresh_hat_label()
            def _refresh_hat_label():
                r = app_state.get('role', 'brainstorm').upper()
                h = app_state.get('hat', 'EXPLORE')
                hat_label.set_text(f'· {r} / {h}')
                hat_label.classes(
                    remove='text-sky-300 text-amber-300 text-emerald-300 '
                           'text-purple-300 text-pink-300 text-gray-300',
                    add=_hat_color(h)
                )

            # ------------------------------------------------
            # OUTPUT TERMINAL
            # ------------------------------------------------

            with ui.scroll_area().classes(
                'bg-black rounded-md w-full flex-grow '
                'border border-gray-800 border-b-0'
            ) as chat_scroll:

                output_display = ui.markdown(
                    'Ready for input...'
                ).classes(
                    'bg-black text-green-400 p-4 '
                    'w-full font-mono text-sm break-words'
                )

            def _agent_label():
                role = app_state.get('role', 'brainstorm')
                init = _ROLE_INITIAL.get(role, 'B')
                style = _ROLE_LABEL_STYLE.get(role, _ROLE_LABEL_STYLE['exec'])
                return f"<span style='{style}'>AI Harmony {init}</span>"

            def render_chat(streaming_text=None):
                md_blocks = []
                for msg in chat_messages:
                    role = msg['role']
                    text = msg['text']
                    if role == 'user':
                        label = (f"<b style='color:{_USER_COLOR}'>Me:</b>")
                        if '```' in text:
                            md_blocks.append(f"{label}\n\n{text}")
                        else:
                            md_blocks.append(
                                f"{label}\n\n"
                                f"<span style='color:{_USER_COLOR}'>"
                                f"{text}</span>"
                            )
                    else:
                        init = msg.get('label', _agent_label())
                        md_blocks.append(
                            f"**{init}:**\n\n{text}"
                        )
                if streaming_text is not None:
                    md_blocks.append(
                        f"**{_agent_label()}:**\n\n{streaming_text}"
                    )

                if not md_blocks:
                    output_display.set_content('Ready for input...')
                else:
                    output_display.set_content('\n\n---\n\n'.join(md_blocks))
                chat_scroll.scroll_to(percent=1.0)
                if streaming_text is None:
                    try:
                        _refresh_info()
                    except Exception:
                        pass

            # ------------------------------------------------
            # INPUT
            # ------------------------------------------------

            # Command suggestions render ABOVE the input (kilo-style upward menu,
            # with a shadow) so it stays usable in fullscreen where the input
            # hugs the bottom edge and there is no room below. No border — the
            # top border read as a thin horizontal bar when it opens.
            sugg_col = ui.column().classes(
                'w-full gap-1 flex-shrink-0 mb-1 '
                'bg-[#0d1117] rounded-lg shadow-lg p-1'
            )
            sugg_col.visible = False

            prompt_input = ui.input(
                placeholder='Ask anything…  ( / commands · ! shell · Enter sends )'
            ).props(
                'dark rounded input-style="color: white;"'
            ).classes(
                'w-full bg-[#0d1117] rounded-2xl mt-1 flex-shrink-0 '
                'border-l-2 border-l-emerald-500 shadow-lg px-4 py-3 text-base'
            )
            prompt_input.on('keydown.enter', lambda e: run_aichat())

            def _set_cmd(cmd):
                prompt_input.value = cmd
                prompt_input.update()
                sugg_col.visible = False
                sugg_col.update()

            def _update_sugg(val):
                try:
                    v = (val or '').strip()
                    if not v.startswith('/') and not v.startswith('!'):
                        sugg_col.visible = False
                        sugg_col.update()
                        return
                    matches = suggest_commands(v)
                    if not matches:
                        sugg_col.visible = False
                        sugg_col.update()
                        return
                    sugg_col.clear()
                    with sugg_col:
                        for m in matches[:8]:
                            ui.button(
                                f"{m['cmd']}  —  {m['desc']}",
                                on_click=lambda m=m: _set_cmd(m['cmd']),
                            ).props('flat dense align=left').classes('w-full text-lg justify-start text-sky-200')
                    sugg_col.visible = True
                    sugg_col.update()
                except Exception as ex:
                    logging.warning("suggest failed: %s", ex)

            prompt_input.on_value_change(lambda e: _update_sugg(e.value))





            async def run_aichat():
                prompt = prompt_input.value.strip()

                if not prompt:
                    ui.notify(
                        'Please enter a prompt',
                        type='warning'
                    )
                    return

                t0 = time.time()

                try:
                    sugg_col.visible = False
                    sugg_col.update()
                except Exception:
                    pass

                # --- SLASH ALIASES (command strip) ---
                if prompt == '/help':
                    prompt_input.value = ''
                    prompt_input.update()
                    chat_messages.append({'role': 'user', 'text': prompt})
                    _hl = '\n'.join(f" `{c['cmd']}` — {c['desc']}" for c in COMMANDS)
                    chat_messages.append({
                        'role': 'assistant',
                        'text': f"**Commands:**\n{_hl}\n\n⏱ {_fmt_dur(time.time() - t0)}",
                        'label': _agent_label()
                    })
                    render_chat()
                    return

                # ------------------------------------------------
                # CLEAR (/clear, /reset — fresh conversation)
                # ------------------------------------------------
                if prompt in ('/clear', '/reset'):
                    prompt_input.value = ''
                    prompt_input.update()
                    try:
                        sess = AICHAT_CONFIG.parent / 'messages.md'
                        if sess.exists():
                            sess.write_text('', encoding='utf-8')
                    except Exception as ex:
                        logging.warning('session clear failed: %s', ex)
                    chat_messages.clear()
                    chat_messages.append({
                        'role': 'assistant',
                        'text': f"🧹 Conversation cleared (session + memory).\n\n⏱ {_fmt_dur(time.time() - t0)}",
                        'label': _agent_label()
                    })
                    render_chat()
                    return

                # ------------------------------------------------
                # PDF COMMAND (/pdf and !pdf — hardwired, no LLM)
                # ------------------------------------------------
                if prompt.startswith('/pdf') or prompt.startswith('!pdf'):
                    pdf_arg = prompt[4:].strip()
                    parsed = parse_pdf_args(pdf_arg)
                    prompt_input.value = ''
                    prompt_input.update()
                    chat_messages.append({'role': 'user', 'text': prompt})
                    _t0pdf = time.time()
                    # No filename -> fall back to the file clicked in the tree.
                    target = resolve_doc_file(
                        parsed.get('file') or '',
                        app_state.get('workdir'),
                        str(BASE_DIR),
                        app_state.get('selected_file'),
                    )
                    if not target:
                        chat_messages.append({
                            'role': 'assistant',
                            'text': (
                                "**PDF lookup failed** — no file attached and "
                                f"no match for `{parsed.get('file') or '(none)'}`.\n\n"
                                "Click a PDF in the tree first, or `!pdf <file>.pdf`.\n\n"
                                f"⏱ {_fmt_dur(time.time() - _t0pdf)}"
                            ),
                            'label': _agent_label(),
                        })
                        render_chat()
                        return

                    name = Path(target).name
                    backend = 'pymupdf'
                    page = parsed.get('page')
                    first = parsed.get('first')
                    last = parsed.get('last')

                    if page or first or last:
                        # Page + paragraph slicing (simple switches).
                        _pt, _paras, _total = pdf_page_paragraphs(
                            target, page or 1)
                        if not _pt:
                            text = ""
                        elif first:
                            text = '\n\n'.join(_paras[:first])
                        elif last:
                            text = '\n\n'.join(_paras[-last:])
                        else:
                            text = _pt
                        scope = (f"page {page or 1}"
                                 + (f", first {first}" if first else "")
                                 + (f", last {last}" if last else ""))
                    else:
                        text, backend = extract_pdf_text(target)
                        scope = 'full'

                    if not text.strip():
                        chat_messages.append({
                            'role': 'assistant',
                            'text': (
                                f"**{name}** — no text on {scope} "
                                f"(scanned images? page out of range?).\n\n"
                                f"⏱ {_fmt_dur(time.time() - _t0pdf)}"
                            ),
                            'label': _agent_label(),
                        })
                        render_chat()
                        return

                    app_state['last_doc'] = target
                    app_state['selected_file'] = target

                    if parsed.get('insert'):
                        # Feed into the NEXT prompt (the terminal body).
                        app_state['last_context'] = (
                            f"[PDF {name} — {scope}]:\n{text[:4000]}"
                        )
                        chat_messages.append({
                            'role': 'assistant',
                            'text': (
                                f"**Inserted** `{name}` ({scope}, "
                                f"{len(text)} chars) into the next prompt.\n\n"
                                f"Just type your question — the model will see it.\n\n"
                                f"⏱ {_fmt_dur(time.time() - _t0pdf)}"
                            ),
                            'label': _agent_label(),
                        })
                        render_chat()
                        return

                    # Write a .md sidecar next to the source so it can be
                    # attached later + edited via the built-in editor.
                    try:
                        md_path = Path(str(target)).with_suffix('.md')
                        md_path.write_text(
                            f"# {name}\n\n"
                            f"<!-- SOURCE: {target} -->\n"
                            f"<!-- BACKEND: {backend} -->\n\n"
                            f"{text}\n",
                            encoding='utf-8',
                        )
                        side_note = f"\n\n📄 sidecar: `{md_path.name}`"
                    except Exception as ex:
                        side_note = f"\n\n(sidecar write failed: {ex})"

                    # Cap the chat echo (the .md sidecar holds the full text).
                    shown = text[:6000]
                    if len(text) > 6000:
                        shown += (f"\n\n… ({len(text)} chars total — "
                                  f"full text in {md_path.name})")

                    chat_messages.append({
                        'role': 'assistant',
                        'text': (
                            f"**PDF → text** (`{name}`, {scope}, via {backend}, "
                            f"{len(text)} chars):**\n\n"
                            f"```text\n{shown}\n```"
                            f"{side_note}\n\n"
                            f"⏱ {_fmt_dur(time.time() - _t0pdf)}"
                        ),
                        'label': _agent_label(),
                    })
                    render_chat()
                    return

                # ------------------------------------------------
                # FLOW BOARD
                # ------------------------------------------------
                if prompt == '/flow':
                    prompt_input.value = ''
                    prompt_input.update()
                    chat_messages.append({'role': 'user', 'text': prompt})
                    try:
                        await render_flow_board()
                        flow_dialog.open()
                        chat_messages.append({
                            'role': 'assistant',
                            'text': 'Flow board rendered from flow.md.',
                            'label': _agent_label()
                        })
                    except Exception as ex:
                        chat_messages.append({
                            'role': 'assistant',
                            'text': f'Flow render failed: {ex}',
                            'label': _agent_label()
                        })
                    render_chat()
                    return

                # ------------------------------------------------
                # SHELL (! command)
                # ------------------------------------------------
                if prompt.startswith('!'):
                    shell_cmd = prompt[1:].strip()
                    if not shell_cmd:
                        ui.notify('Empty shell command', type='warning')
                        return
                    # Dash aliases: `!ollama-list` / `!Ollama-ps` → `ollama list/ps`.
                    _ALIAS = {'ollama-list': 'ollama list', 'ollama-ps': 'ollama ps',
                              'ollama-show': 'ollama show', 'aichat-version': 'aichat --version'}
                    _aw = shell_cmd.split()
                    if _aw and _aw[0].lower() in _ALIAS:
                        shell_cmd = _ALIAS[_aw[0].lower()] + (' ' + ' '.join(_aw[1:]) if len(_aw) > 1 else '')
                    # !master = escalated full shell (sudo allowed). Plain !
                    # refuses sudo so a password prompt can never hang the
                    # panel — use !master for anything needing root.
                    _master = shell_cmd.lower().startswith('master ')
                    if _master:
                        shell_cmd = shell_cmd[7:].strip()
                        if not shell_cmd:
                            ui.notify('Empty master command', type='warning')
                            return
                        _tag = 'MASTER'
                    else:
                        _tag = 'Shell'
                        # Normal `!` = basic functions only: read-only +
                        # cp/mv/mkdir/touch + ollama list/ps. Anything
                        # destructive, redirecting, or chained goes to !master.
                        _NORMAL_OK = {
                            'ls', 'cat', 'head', 'tail', 'pwd', 'echo',
                            'grep', 'egrep', 'fgrep', 'find', 'wc', 'diff',
                            'file', 'stat', 'du', 'df', 'lsblk', 'tree',
                            'cp', 'mv', 'mkdir', 'touch', 'ollama', 'aichat',
                            'id', 'whoami', 'hostname', 'uname', 'date',
                        }
                        _first = (shell_cmd.split() or [''])[0].strip().lower()
                        _ok = _first in _NORMAL_OK
                        if _ok and _first == 'ollama':
                            _sub = (shell_cmd.split()[1:2] or [''])[0].lower()
                            _ok = _sub in ('list', 'ps', 'show', '--version',
                                           '-h', '--help', '')
                        if _ok and re.search(r'[;&`$()<>]', shell_cmd):
                            _ok = False  # chains/subshells/redirects = master
                        if not _ok:
                            chat_messages.append({'role': 'user', 'text': prompt})
                            chat_messages.append({
                                'role': 'assistant',
                                'text': (f"`{shell_cmd.split()[0] if shell_cmd.split() else ''}` isn't a basic `!` command.\n"
                                         "Basic: `ls cat head tail grep find cp mv mkdir touch ollama list/ps` (+ pipes).\n"
                                         "For the rest — deletes, redirects, `;`/`&&` chains, python, sudo — use `!master ...`."),
                                'label': _agent_label()
                            })
                            prompt_input.value = ''
                            prompt_input.update()
                            render_chat()
                            return
                        if re.search(r'(^|[\s;&|])sudo(\s|$)', shell_cmd):
                            chat_messages.append({'role': 'user', 'text': prompt})
                            chat_messages.append({
                                'role': 'assistant',
                                'text': ("**sudo blocked in `!`** — a password prompt would hang the panel.\n"
                                         "Use `!master sudo ...` instead (non-interactive, fails fast if "
                                         "a password is needed — add a NOPASSWD rule for that binary)."),
                                'label': _agent_label()
                            })
                            prompt_input.value = ''
                            prompt_input.update()
                            render_chat()
                            return
                    prompt_input.value = ''
                    prompt_input.update()
                    chat_messages.append({'role': 'user', 'text': prompt})
                    try:
                        if _master and shell_cmd.startswith('sudo ') and ' -n' not in shell_cmd.split():
                            # Non-interactive: fail fast instead of hanging on
                            # a password prompt the panel can never answer.
                            shell_cmd = 'sudo -n ' + shell_cmd[5:]
                        proc = await asyncio.create_subprocess_shell(
                            shell_cmd,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            cwd=app_state.get('workdir'),
                        )
                        out, err = await proc.communicate()
                        out = out.decode(errors='replace')
                        err = err.decode(errors='replace')
                        if proc.returncode == 0:
                            body = out.strip() or 'Done (no output)'
                        else:
                            body = err.strip() or out.strip() or 'Done (no output)'
                            if _master and 'a password is required' in body:
                                body += ('\n\nHint: `sudo -n` needs a NOPASSWD rule, e.g.\n'
                                         '`sudo visudo` → `joao ALL=(ALL) NOPASSWD: /usr/bin/apt`')
                        chat_messages.append({
                            'role': 'assistant',
                            'text': f"**{_tag} (`{shell_cmd}`):**\n```text\n{body}\n```\n\n⏱ {_fmt_dur(time.time() - t0)}",
                            'label': _agent_label()
                        })
                        app_state['last_context'] = f"[{_tag} {shell_cmd}]:\n{body}"
                    except Exception as ex:
                        chat_messages.append({
                            'role': 'assistant',
                            'text': f"**{_tag} (`{shell_cmd}`) failed:**\n```text\n{ex}\n```",
                            'label': _agent_label()
                        })
                    render_chat()
                    return

                # ------------------------------------------------
                # FAST LOCAL commands (zero-LLM — never send to the model).
                # Bare "ollama list" used to burn 75s in phi3; run it here
                # in ~0.5s. Read-only allowlist only; everything else still
                # needs the `!` prefix or goes to the model.
                # ------------------------------------------------
                _fast = (prompt or '').strip().lower()
                _FAST_LOCAL = {
                    'ollama list': 'ollama list',
                    'ollama ps': 'ollama ps',
                    'list models': 'ollama list',
                    'show models': 'ollama list',
                    'what models': 'ollama list',
                    'models': 'ollama list',
                }
                if _fast in _FAST_LOCAL:
                    shell_cmd = _FAST_LOCAL[_fast]
                    prompt_input.value = ''
                    prompt_input.update()
                    chat_messages.append({'role': 'user', 'text': prompt})
                    try:
                        proc = await asyncio.create_subprocess_shell(
                            shell_cmd,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            cwd=app_state.get('workdir'),
                        )
                        out, err = await proc.communicate()
                        out = out.decode(errors='replace')
                        err = err.decode(errors='replace')
                        body = (out.strip() if proc.returncode == 0
                                else (err.strip() or out.strip())) or 'Done (no output)'
                        chat_messages.append({
                            'role': 'assistant',
                            'text': f"**{shell_cmd}:**\n```text\n{body}\n```\n\n⏱ {_fmt_dur(time.time() - t0)}",
                            'label': _agent_label()
                        })
                    except Exception as ex:
                        chat_messages.append({
                            'role': 'assistant',
                            'text': f"**{shell_cmd} failed:**\n```text\n{ex}\n```",
                            'label': _agent_label()
                        })
                    render_chat()
                    return

                # Prevent accidentally launching multiple
                # model processes at once.
                if app_state["process"] is not None:
                    ui.notify(
                        'A model process is already running.',
                        type='warning'
                    )
                    return

                # Clear the box immediately so the user can keep typing.
                prompt_input.value = ''
                prompt_input.update()

                # Record the user's turn in the visible history.
                chat_messages.append({'role': 'user', 'text': prompt})

                full_prompt = build_model_prompt(prompt)
                _refresh_hat_label()

                cmd = [
                    _aichat_bin(),
                    '-r',
                    app_state["role"],
                    '-s',
                    app_state["role"],
                ]

                _sel = app_state.get("selected_file")
                if _sel:
                    try:
                        _attach = os.path.isfile(_sel) and os.path.getsize(_sel) <= 30000
                    except Exception:
                        _attach = False
                    if _attach:
                        cmd.extend(['-f', _sel])
                    else:
                        # a clicked file that's huge (or now missing) was silently
                        # blowing every turn past the model context
                        # ("Exceed max_input_tokens limit") — stop re-attaching it.
                        app_state["selected_file"] = None
                        try:
                            selected_file_label.text = 'No file selected'
                        except Exception:
                            pass
                        safe_notify('Attached file too large (or gone) — cleared. Click a small file to attach.', type='warning')

                # DOC PATH (native aichat `-f`): when the prompt names a doc
                # (e.g. "extract the body from the RFP pdf at X folder"),
                # attach the matched file(s) so aichat extracts them natively
                # (it reads PDF text itself — verified). Single files only,
                # size-capped in find_doc_attach; never a whole directory.
                try:
                    for doc in find_doc_attach(prompt, str(BASE_DIR)):
                        if doc != app_state["selected_file"]:
                            cmd.extend(['-f', doc])
                except Exception as ex:
                    logging.warning("doc attach failed: %s", ex)

                cmd.append(full_prompt)

                env = os.environ.copy()

                debug_enabled = bool(
                    debug_checkbox.value
                )

                if debug_enabled:
                    env["RUST_LOG"] = "debug"

                logging.info(
                    "Executing: %s",
                    ' '.join(cmd)
                )

                logging.info(
                    "Working Directory: %s",
                    app_state["workdir"]
                )

                render_chat('▌')

                _t0 = time.time()

                process = None

                try:

                    # ----------------------------------------
                    # START PROCESS
                    # ----------------------------------------

                    process = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=app_state["workdir"],
                        env=env,
                    )

                    app_state["process"] = process

                    logging.info(
                        "aichat started with PID %s",
                        process.pid
                    )

                    # ----------------------------------------
                    # STREAM STATE
                    # ----------------------------------------

                    stdout_buffer = []
                    stderr_buffer = []

                    last_ui_update = 0.0

                    # ----------------------------------------
                    # READ STDERR IN PARALLEL
                    # ----------------------------------------

                    async def read_stderr():

                        while True:

                            chunk = await process.stderr.read(4096)

                            if not chunk:
                                break

                            stderr_buffer.append(
                                chunk.decode(
                                    errors='replace'
                                )
                            )

                    stderr_task = asyncio.create_task(
                        read_stderr()
                    )

                    # ----------------------------------------
                    # STREAM STDOUT
                    # ----------------------------------------

                    while True:

                        chunk = await process.stdout.read(4096)

                        if not chunk:
                            break

                        text = chunk.decode(
                            errors='replace'
                        )

                        stdout_buffer.append(text)

                        # ------------------------------------
                        # THROTTLE UI UPDATES
                        #
                        # We don't want to ask NiceGUI/browser
                        # to redraw for every tiny token.
                        # ------------------------------------

                        now = asyncio.get_running_loop().time()

                        if now - last_ui_update >= 0.10:

                            current_text = clean_model_output(
                                ''.join(stdout_buffer)
                            )

                            render_chat(current_text or '▌')

                            last_ui_update = now

                    # ----------------------------------------
                    # PROCESS FINISHED
                    # ----------------------------------------

                    await process.wait()

                    await stderr_task

                    final_output = clean_model_output(
                        ''.join(stdout_buffer)
                    )

                    stderr_output = ''.join(
                        stderr_buffer
                    )

                    logging.info(
                        'aichat finished: return code %s',
                        process.returncode
                    )

                    # ----------------------------------------
                    # DEBUG OUTPUT
                    # ----------------------------------------

                    if debug_enabled:

                        debug_header = (
                            '### [DEBUG TELEMETRY]\n'
                            f'- **Working Dir:** '
                            f'`{app_state["workdir"]}`\n'
                            f'- **Role:** '
                            f'`{app_state["role"]}`\n'
                            f'- **Hat:** '
                            f'`{app_state.get("hat", "EXPLORE")}`\n'
                            f'- **Return Code:** '
                            f'`{process.returncode}`\n'
                            f'- **Environment:** '
                            f'`RUST_LOG=debug`\n'
                            '\n'
                            '**Stderr Stream:**\n'
                            '```text\n'
                            f'{stderr_output or "None"}\n'
                            '```\n'
                            '---\n'
                        )

                        result_text = (
                            debug_header +
                            '**Output:**\n'
                            '```text\n'
                            f'{final_output}\n'
                            '```'
                        )


                    # ----------------------------------------
                    # NORMAL OUTPUT
                    # ----------------------------------------

                    elif process.returncode == 0:

                        if wrap_checkbox.value:
                            result_text = final_output
                        else:
                            result_text = (
                                '```text\n'
                                f'{final_output}\n'
                                '```'
                            )


                    else:

                        result_text = (
                            '```text\n'
                            'aichat failed.\n\n'
                            f'{stderr_output or final_output}\n'
                            '```'
                        )

                    # Commit the assistant's finished reply to history and
                    # re-render (no more live-streamed/streaming tail).
                    _elapsed = time.time() - _t0
                    chat_messages.append({
                        'role': 'assistant',
                        'text': result_text
                        + f'\n\n⏱ {_fmt_dur(_elapsed)}',
                        'label': _agent_label(),
                    })
                    render_chat()

                except asyncio.CancelledError:

                    if process is not None:

                        try:
                            process.terminate()
                        except ProcessLookupError:
                            pass

                    raise

                except Exception as ex:

                    logging.exception(
                        'aichat execution failed'
                    )

                    chat_messages.append({
                        'role': 'assistant',
                        'text': f'```text\nExecution failed:\n{ex}\n```',
                        'label': _agent_label(),
                    })
                    render_chat()

                finally:

                    app_state["process"] = None

            # ------------------------------------------------
            # ACTION BUTTONS
            # ------------------------------------------------

            with ui.row().classes(
                'gap-3 w-full justify-between '
                'items-center mt-2 flex-shrink-0'
            ):

                execute_button = ui.button(
                    'Execute via aichat',
                    on_click=run_aichat
                ).classes(
                    'bg-blue-600 hover:bg-blue-500 '
                    'text-white font-medium px-6'
                )

                async def _open_flow():
                    flow_dialog.open()
                    await render_flow_board()

                ui.button(
                    '📊 Flow',
                    on_click=_open_flow
                ).classes(
                    'bg-emerald-700 text-white font-medium px-4'
                ).tooltip('Open flow board (flow.md)')

                ui.button(
                    '⚙️ Control Center',
                    on_click=lambda: _open_cc_menu()
                ).classes(
                    'bg-gray-700 text-gray-200 font-medium px-4'
                )

        # ====================================================
        # INFO PANEL (session info column, right side)
        # ====================================================

        info_panel = ui.column().classes(
            'w-72 h-full bg-gray-800/60 p-3 rounded-lg '
            'border border-gray-700 overflow-y-auto flex-shrink-0'
        )

        with info_panel:
            ui.label('Session Info').classes(
                'text-bold text-base text-sky-300'
            )
            info_role_label = ui.label('').classes(
                'text-xs font-mono text-gray-200'
            )
            info_hat_label = ui.label('').classes(
                'text-xs font-mono text-gray-200'
            )
            info_model_label = ui.label('').classes(
                'text-xs font-mono text-emerald-300'
            )
            info_ping_label = ui.label('').classes(
                'text-xs font-mono text-sky-300'
            )
            info_project_label = ui.label('').classes(
                'text-xs font-mono text-gray-200'
            )
            info_file_label = ui.label('').classes(
                'text-xs font-mono text-gray-200'
            )
            info_workdir_label = ui.label('').classes(
                'text-xs font-mono text-gray-200'
            )
            ui.separator().classes('bg-gray-700')
            ui.label('Prompt context (chars)').classes(
                'text-xs font-semibold text-gray-400'
            )
            info_ctx_label = ui.label('').classes(
                'text-xs font-mono text-gray-400'
            )

        info_panel.visible = False

        def _refresh_info():
            try:
                if info_panel is None:
                    return
                r = (app_state.get('role') or 'brainstorm').upper()
                h = app_state.get('hat') or 'EXPLORE'
                proj = app_state.get('active_project') or '(none)'
                f = (Path(app_state['selected_file']).name
                     if app_state.get('selected_file') else '(none)')
                wd = (Path(app_state['workdir']).name
                      if app_state.get('workdir') else '(none)')
                info_role_label.set_text(f'Role: {r}')
                info_hat_label.set_text(f'Hat: {h}')
                try:
                    lm = app_state.get('loading_model')
                    if lm:
                        info_model_label.set_text(f'Model: ⏳ {lm}...')
                    else:
                        loaded = _mc.loaded_names() if HAS_MC else []
                        info_model_label.set_text(
                            f"Model: {loaded[0] if loaded else '(none in RAM)'}")
                except Exception:
                    info_model_label.set_text('Model: (unknown)')
                try:
                    lp = app_state.get('last_ping')
                    if lp:
                        state = '⚡ warm' if lp.get('warm') else '⏳ cold'
                        ttft = f"{lp['ttft']:.1f}s" if lp.get('ttft') else 'n/a'
                        info_ping_label.set_text(
                            f"Ping: {state} [{lp.get('family', '?')}] "
                            f"first-token {ttft} · total {lp['total']:.1f}s"
                        )
                    else:
                        info_ping_label.set_text('Ping: (run ⚡ Ping in Control Center)')
                except Exception:
                    info_ping_label.set_text('')
                info_project_label.set_text(f'Project: {proj}')
                info_file_label.set_text(f'File: {f}')
                info_workdir_label.set_text(f'Folder: {wd}')
                ctx = app_state.get('last_ctx')
                if ctx:
                    info_ctx_label.set_text(
                        f"root {ctx.get('root', 0)} · "
                        f"here {ctx.get('here', 0)} · "
                        f"proj {ctx.get('proj', 0)} · "
                        f"viking {ctx.get('viking', 0)} · "
                        f"state {ctx.get('state', 0)} · "
                        f"req {ctx.get('req', 0)}"
                    )
                else:
                    info_ctx_label.set_text('(no prompt yet)')
            except Exception as ex:
                logging.warning('info refresh failed: %s', ex)

        _refresh_info()


# ============================================================
# MODELS CONTROL CENTER (thin UI — engine lives in aichat_model_controller)
# ============================================================

_WHITE_BTN = 'background:#ffffff!important;color:#333333;font-weight:normal'


def _white_stub(name):
    """Unwired button: white, announces itself, no crash."""
    return ui.button(name, on_click=lambda: ui.notify(f'{name}: not wired yet', type='warning')).style(_WHITE_BTN)


def _mc_guard():
    if not HAS_MC:
        ui.notify('Model controller module missing', type='negative')
        return False
    return True


def _open_cc_menu():
    with ui.dialog() as dlg, ui.card().classes('p-4 gap-2 min-w-72 dialog-drag'):
        ui.label('⚙️ Control Center').classes('text-lg font-bold text-emerald-400 drag-handle')
        ui.button('Sys & Cld Selection', on_click=lambda: (dlg.close(), _cc_models())).classes('w-full')
        ui.button('Model Dwnl', on_click=lambda: (dlg.close(), _cc_download())).classes('w-full')
        ui.button('Diagnostics', on_click=lambda: (dlg.close(), _cc_diag())).classes('w-full')
        ui.button('Vault', on_click=lambda: (dlg.close(), _cc_vault())).classes('w-full')
    dlg.open()


# --- 1. Sys & Cld Selection -------------------------------------------------

def _cc_models():
    if not _mc_guard():
        return
    with ui.dialog() as dlg, ui.card().classes('p-4 gap-2 min-w-96 dialog-drag'):
        ui.label('Sys & Cld Selection').classes('text-lg font-bold text-emerald-400 drag-handle')
        loaded_lbl = ui.label('').classes('text-xs text-yellow-400 font-mono')
        ping_lbl = ui.label('').classes('text-xs text-sky-300 font-mono')
        prog_lbl = ui.label('').classes('text-xs font-mono text-yellow-300')
        prog_bar = ui.linear_progress(show_value=False).classes('w-full')
        prog_bar.visible = False
        sel = ui.select([], label='Ollama model').classes('w-full')

        def _show_progress(text=''):
            try:
                prog_bar.visible = True
                if text:
                    prog_lbl.text = text
                safe_update(prog_lbl)
                safe_update(prog_bar)
            except Exception:
                pass

        def _hide_progress():
            try:
                prog_bar.visible = False
                prog_lbl.text = ''
                safe_update(prog_lbl)
                safe_update(prog_bar)
            except Exception:
                pass

        gguf_paths = {}

        def _refresh():
            models = _mc.ollama_models()
            tags = [m['name'] for m in models]
            g = _mc.scan_gguf_folder(str(BASE_DIR))
            gguf_paths.clear()
            for n, p in g.items():
                if not _mc.gguf_is_imported(n, models):
                    gguf_paths[n] = p
            opts = sorted(_mc.tag_local(t) for t in tags)
            opts += sorted(_mc.tag_local(f'{n} (gguf-only)') for n in gguf_paths)
            # Preserve the selection across refreshes (set_options clears it,
            # stranding Unload/Ping/Tune on "Pick a model first").
            _keep = sel.value if sel.value in opts else None
            sel.set_options(opts or ['(none — is Ollama running?)'])
            if _keep:
                sel.value = _keep
            sel.update()
            loaded = _mc.loaded_names()
            loaded_lbl.text = f"in RAM: {', '.join(loaded) if loaded else '(none)'}"
            ui.notify('Model list refreshed')

        def _gguf_key_from_value(v):
            if not v or '(gguf-only)' not in v:
                return None
            return v.replace('[LCL] ', '').replace(' (gguf-only)', '').strip()

        def _prompt_import(gguf_path):
            stem = Path(gguf_path).stem
            suggested = (re.sub(r'[^a-z0-9]+', '-', stem.lower()).strip('-')
                         or 'imported-model')
            with ui.dialog() as ndlg, ui.card().classes('p-4 gap-2 min-w-80 dialog-drag'):
                ui.label(f'Import {Path(gguf_path).name}').classes(
                    'font-bold text-emerald-400 drag-handle')
                ui.label('No Ollama entry yet — give it a name:').classes(
                    'text-xs text-gray-400')
                name_in = ui.input('Model name', value=suggested).classes('w-full')
                ui.label(f'GGUF: {gguf_path}').classes(
                    'text-[10px] font-mono text-gray-500 break-all')
                import threading

                def _go():
                    mname = name_in.value.strip() or suggested
                    ndlg.close()
                    app_state['loading_model'] = mname
                    _refresh_model_status()

                    def _run():
                        _show_progress(f'importing {mname}...')

                        def _line(txt):
                            _show_progress(txt)

                        ok = _mc.import_single_model(mname, gguf_path, on_line=_line)
                        if ok:
                            _show_progress(f'imported {mname} — loading...')
                            _mc.load_model(mname, timeout=900, base_dir=str(BASE_DIR))
                            app_state['loading_model'] = None
                            _refresh_model_status()
                            _hide_progress()
                            try:
                                _mc.set_aichat_model(mname)
                            except Exception:
                                pass
                        else:
                            app_state['loading_model'] = None
                            _refresh_model_status()
                            _hide_progress()
                            safe_notify(f'Import failed for {mname}',
                                        type='negative', timeout=8000)
                        try:
                            if ok:
                                try:
                                    sel.value = _mc.tag_local(mname)
                                except Exception:
                                    pass
                            _refresh()
                        except Exception:
                            pass
                    threading.Thread(target=_run, daemon=True).start()

                ui.button('Import + Load', on_click=_go).classes('bg-emerald-600 text-white')
                ui.button('Cancel', on_click=ndlg.close).props('flat')
            ndlg.open()

        def _load_launch():
            name = sel.value
            if not name or name.startswith('(none'):
                ui.notify('Pick a model first', type='warning')
                return
            gguf_key = _gguf_key_from_value(sel.value)
            if gguf_key and gguf_key in gguf_paths:
                _prompt_import(gguf_paths[gguf_key])
                return
            ui.notify(f'Loading {name} — slow models take minutes...', type='info')
            app_state['loading_model'] = name
            _refresh_model_status()
            import threading

            def _go():
                _show_progress(f'loading {name}...')
                ok, msg = _mc.load_model(name, timeout=900, base_dir=str(BASE_DIR))
                app_state['loading_model'] = None
                _refresh_model_status()
                _hide_progress()
                safe_notify(msg, type='positive' if ok else 'negative', timeout=10000)
                # Point the chat CLI at this model too, so Load & Launch
                # actually changes what aichat uses (not just what's in RAM).
                try:
                    ok2, msg2 = _mc.set_aichat_model(name)
                    safe_notify(msg2, type='positive' if ok2 else 'warning', timeout=8000)
                except Exception as ex:
                    safe_notify(f'aichat config update failed: {ex}', type='warning', timeout=8000)
                try:
                    loaded_lbl.text = f"in RAM: {', '.join(_mc.loaded_names()) or '(none)'}"
                except Exception:
                    pass
            threading.Thread(target=_go, daemon=True).start()

        def _ping():
            name = sel.value
            if not name or name.startswith('(none'):
                ui.notify('Pick a model first', type='warning')
                return
            ping_lbl.text = f'pinging {name} (real workload)...'
            import threading

            def _go():
                ok, rep = _mc.ping_model(name)
                if ok:
                    _mc.log_ping(rep, str(BASE_DIR))
                    ttft = f"{rep['ttft']:.1f}s" if rep.get('ttft') else 'n/a'
                    state = '⚡ warm' if rep.get('warm') else '⏳ cold'
                    ping_lbl.text = (
                        f"{state} {rep['model']} [{rep.get('family', '?')}] — "
                        f"first token {ttft} · total {rep['total']:.1f}s"
                    )
                    ui.notify(
                        f"{state} {rep['model']} first token {ttft}, total {rep['total']:.1f}s",
                        type='positive', timeout=8000)
                    # Keep the info panel's ping line fresh.
                    try:
                        app_state['last_ping'] = rep
                        _refresh_info()
                    except Exception:
                        pass
                else:
                    ping_lbl.text = f"ping failed: {rep.get('error', '?')}"
                    ui.notify(f"Ping failed: {rep.get('error', '?')}",
                              type='negative', timeout=8000)
            threading.Thread(target=_go, daemon=True).start()

        def _unload():
            name = sel.value
            if not name or name.startswith('(none'):
                # Single-model box: fall back to whatever is actually resident.
                try:
                    _res = _mc.loaded_names()
                except Exception:
                    _res = []
                if _res:
                    name = _res[0]
                else:
                    ui.notify('Pick a model first', type='warning')
                    return
            _mc.unload_model(name)
            loaded_lbl.text = f"in RAM: {', '.join(_mc.loaded_names()) or '(none)'}"
            ui.notify(f'Unloaded {name}')

        def _tune():
            name = sel.value
            if not name or name.startswith('(none'):
                ui.notify('Pick a model first', type='warning')
                return
            cur = _mc.get_tuning(str(BASE_DIR), name)
            with ui.dialog() as tdlg, ui.card().classes('p-4 gap-2 min-w-72 dialog-drag'):
                ui.label(f'Tune {name}').classes('font-bold text-emerald-400 drag-handle')
                ctx_in = ui.input('num_ctx', value=str(cur.get('num_ctx', ''))).classes('w-full')
                thr_in = ui.input('num_thread', value=str(cur.get('num_thread', ''))).classes('w-full')
                tmp_in = ui.input('temperature', value=str(cur.get('temperature', ''))).classes('w-full')

                def _save():
                    def _num(v):
                        try:
                            return float(v) if v not in (None, '') else None
                        except Exception:
                            return None
                    ok = _mc.set_tuning(str(BASE_DIR), name, {
                        'num_ctx': _num(ctx_in.value),
                        'num_thread': _num(thr_in.value),
                        'temperature': _num(tmp_in.value)})
                    ui.notify('Tuning saved (applies at next load)' if ok else 'Save failed',
                              type='positive' if ok else 'negative')
                    tdlg.close()
                ui.button('Save', on_click=_save).classes('bg-emerald-600 text-white')
            tdlg.open()

        def _delete_traces():
            name = sel.value
            if not name or name.startswith('(none'):
                ui.notify('Pick a model first', type='warning')
                return
            with ui.dialog() as cdlg, ui.card().classes('p-4 gap-2 dialog-drag'):
                ui.label(f"Delete ALL traces of {name}?").classes('font-bold text-red-400 drag-handle')
                ui.label('Removes: Ollama entry + blobs, GGUF file, Modelfile, tuning. Cannot be undone.').classes('text-xs')
                ui.button('YES, delete everything',
                          on_click=lambda: (cdlg.close(),
                                            ui.notify(_mc.delete_model_all_traces(name, str(BASE_DIR))[1],
                                                      timeout=8000),
                                            _refresh())).props('color=red')
                ui.button('Cancel', on_click=cdlg.close)
            cdlg.open()

        with ui.row().classes('gap-2 flex-wrap'):
            ui.button('Refresh models', on_click=_refresh).classes('bg-emerald-600 text-white')
            ui.button('Load & Launch', on_click=_load_launch).classes('bg-blue-600 text-white')
            ui.button('⚡ Ping', on_click=_ping).classes('bg-sky-600 text-white')
            ui.button('Unload', on_click=_unload).classes('bg-gray-600 text-white')
            ui.button('Tune', on_click=_tune).classes('bg-purple-600 text-white')
            ui.button('Delete all traces', on_click=_delete_traces).props('color=red')
            _white_stub('Team Check')
            _white_stub('Fastest Cloud')
        ui.button('Close', on_click=dlg.close).props('flat')
    _refresh()
    dlg.open()


# --- 2. Model Download (HF) -------------------------------------------------

def _cc_download():
    if not _mc_guard():
        return
    with ui.dialog() as dlg, ui.card().classes('p-4 gap-2 w-[40rem] max-w-[94vw] dialog-drag'):
        ui.label('Model Dwnl (HuggingFace → Ollama)').classes('text-lg font-bold text-emerald-400 drag-handle')
        org_in = ui.input('Provider/Org (e.g. google)').classes('w-full')
        q_in = ui.input('Search (optional, e.g. gemma)').classes('w-full')
        prog_lbl = ui.label('').classes('text-xs text-yellow-400 font-mono')
        auto_imp = ui.checkbox('Auto-import to Ollama', value=True)
        del_gguf = ui.checkbox('Delete GGUF after import', value=True)
        _repo = {'id': '', 'files': {}}

        ui.label('Results — click a repo').classes('text-xs text-gray-400 mt-1')
        res_scroll = ui.scroll_area().classes('w-full h-52 border border-gray-700 rounded')
        res_col = ui.column().classes('w-full gap-1')

        ui.label('GGUF file — click to select').classes('text-xs text-gray-400 mt-1')
        file_scroll = ui.scroll_area().classes('w-full h-40 border border-gray-700 rounded')
        file_col = ui.column().classes('w-full gap-1')
        _picked_file = {'name': ''}

        def _search():
            rows = _mc.search_hf_models(org_in.value.strip(), q_in.value.strip())
            res_col.clear()
            with res_col:
                if not rows or (len(rows) == 1 and rows[0][0].startswith(('No models', 'Search failed'))):
                    ui.label(rows[0][0] if rows else 'No results').classes('text-gray-400 text-xs p-2')
                for r, dl in rows:
                    ui.button(
                        f'{r}  ·  {dl:,} downloads',
                        on_click=lambda r=r: _pick_repo(r),
                    ).props('flat dense align=left').classes('w-full justify-start text-xs text-sky-200')
            ui.notify(f'{len(rows)} result(s)')

        def _pick_repo(repo_id):
            _repo['id'] = repo_id
            files = _mc.list_gguf_files(repo_id)
            _repo['files'] = dict(files)
            file_col.clear()
            _picked_file['name'] = ''
            with file_col:
                if not files:
                    ui.label('No GGUF files in this repo').classes('text-gray-400 text-xs p-2')
                for f, _sz in files:
                    ui.button(
                        f,
                        on_click=lambda f=f: _pick_file(f),
                    ).props('flat dense align=left').classes('w-full justify-start text-xs text-emerald-200')

        def _pick_file(name):
            _picked_file['name'] = name
            ui.notify(f'Selected: {name}')

        def _download():
            if not _repo['id'] or not _picked_file['name']:
                ui.notify('Search → click repo → click file first', type='warning')
                return
            prog_lbl.text = 'downloading... (minutes for GB files)'
            import threading

            def _go():
                ok, msg = _mc.download_hf_file(_repo['id'], _picked_file['name'], str(BASE_DIR),
                                                auto_import=auto_imp.value,
                                                delete_after=del_gguf.value,
                                                on_progress=lambda p: prog_lbl.set_text(f'{p}%'))
                ui.notify(msg, type='positive' if ok else 'negative', timeout=10000)
                prog_lbl.text = 'done.' if ok else 'failed.'
            threading.Thread(target=_go, daemon=True).start()

        with ui.row().classes('gap-2 flex-wrap'):
            ui.button('Search', on_click=_search).classes('bg-emerald-600 text-white')
            ui.button('Download selected file', on_click=_download).classes('bg-blue-600 text-white')
        ui.button('Close', on_click=dlg.close).props('flat')
    dlg.open()


# --- 3. Diagnostics ---------------------------------------------------------

def _cc_diag():
    if not _mc_guard():
        return
    with ui.dialog() as dlg, ui.card().classes('p-4 gap-2 min-w-96 dialog-drag'):
        ui.label('Diagnostics').classes('text-lg font-bold text-emerald-400 drag-handle')
        out = ui.textarea('').classes('w-full font-mono').props('rows=10 readonly')

        def _run():
            tags = _mc.ollama_tags()
            ps = _mc.ollama_ps()
            g = _mc.scan_gguf_folder(str(BASE_DIR))
            lines = [f"Ollama reachable: {'yes' if tags else 'NO — start ollama serve?'}",
                     f"imported models ({len(tags)}): {', '.join(_mc.tag_local(t) for t in tags) or '(none)'}",
                     f"in RAM ({len(ps)}): {', '.join(m.get('name','?') for m in ps) or '(none)'}",
                     f"GGUF on disk ({len(g)}): {', '.join(sorted(g)) or '(none)'}"]
            try:
                import shutil
                u = shutil.disk_usage(str(BASE_DIR))
                lines.append(f"disk free: {u.free // 2**30}G")
            except Exception:
                pass
            out.value = '\n'.join(lines)
        ui.button('Refresh', on_click=_run).classes('bg-emerald-600 text-white')
        ui.button('Close', on_click=dlg.close).props('flat')
    _run()
    dlg.open()


# --- 4. Vault ---------------------------------------------------------------

def _cc_vault():
    if not HAS_VAULT:
        ui.notify('auth_store.py missing', type='negative')
        return
    with ui.dialog() as dlg, ui.card().classes('p-4 gap-2 min-w-96 dialog-drag'):
        ui.label('🔑 Credentials Vault').classes('text-lg font-bold text-emerald-400 drag-handle')
        st_lbl = ui.label('').classes('text-xs font-mono')
        svc_sel = ui.select([], label='Services').classes('w-full')

        def _refresh():
            try:
                unlocked = _vault.is_unlocked()
            except Exception:
                unlocked = False
            st_lbl.text = f"status: {'UNLOCKED' if unlocked else 'LOCKED'}"
            try:
                svcs = _vault.services() if unlocked else []
            except Exception:
                svcs = []
            svc_sel.set_options(svcs or ['(locked or empty)'])
            svc_sel.update()

        def _unlock_create():
            with ui.dialog() as udlg, ui.card().classes('p-4 gap-2 min-w-72 dialog-drag'):
                ui.label('🔑 Unlock / Create vault').classes('font-bold text-emerald-400 drag-handle')
                pw = ui.input('Passphrase', password=True).classes('w-full')
                pw2 = ui.input('Confirm (create only)', password=True).classes('w-full')

                def _go():
                    try:
                        if _vault.is_unlocked():
                            _vault.lock()
                            ui.notify('Vault locked')
                        elif pw2.value:
                            if pw.value != pw2.value:
                                ui.notify('Passphrases differ', type='warning')
                                return
                            _vault.create_vault(pw.value)
                            _vault.unlock(pw.value)
                            ui.notify('Vault created + unlocked', type='positive')
                        else:
                            _vault.unlock(pw.value)
                            ui.notify('Vault unlocked', type='positive')
                    except Exception as e:
                        ui.notify(f'Vault error: {e}', type='negative')
                    udlg.close()
                    _refresh()
                ui.button('Unlock / Create / Lock-toggle', on_click=_go).classes('bg-emerald-600 text-white')
            udlg.open()

        def _add_service():
            with ui.dialog() as adlg, ui.card().classes('p-4 gap-2 min-w-72 dialog-drag'):
                ui.label('Add service').classes('font-bold text-emerald-400 drag-handle')
                name_in = ui.input('Service (e.g. deepseek)').classes('w-full')
                key_in = ui.input('API key', password=True).classes('w-full')
                url_in = ui.input('Base URL (optional)').classes('w-full')

                def _save():
                    try:
                        _vault.set_credential(name_in.value.strip(), 'api_key', key_in.value)
                        if url_in.value.strip():
                            _vault.set_credential(name_in.value.strip(), 'base_url', url_in.value.strip())
                        ui.notify(f"Service '{name_in.value.strip()}' saved", type='positive')
                    except Exception as e:
                        ui.notify(f'Save error: {e}', type='negative')
                    adlg.close()
                    _refresh()
                ui.button('Save service', on_click=_save).classes('bg-emerald-600 text-white')
            adlg.open()

        def _delete_service():
            s = svc_sel.value
            if not s or s.startswith('(locked'):
                ui.notify('Pick a service first', type='warning')
                return
            try:
                _vault.remove_service(s)
                ui.notify(f"Service '{s}' removed")
            except Exception as e:
                ui.notify(f'Delete error: {e}', type='negative')
            _refresh()

        def _test_email():
            try:
                ok, msg = _vault.send_test_email()
                ui.notify(msg, type='positive' if ok else 'negative', timeout=8000)
            except Exception as e:
                ui.notify(f'Email error: {e}', type='negative')

        def _import_opencode():
            if not _mc_guard():
                return
            ok, msg = _mc.import_from_opencode()
            ui.notify(msg, type='positive' if ok else 'negative', timeout=8000)
            _refresh()

        with ui.row().classes('gap-2 flex-wrap'):
            ui.button('Unlock / Create', on_click=_unlock_create).classes('bg-emerald-600 text-white')
            ui.button('Lock now', on_click=lambda: (_vault.lock(), _refresh(), ui.notify('Vault locked'))).classes('bg-gray-600 text-white')
            ui.button('Add service', on_click=_add_service).classes('bg-blue-600 text-white')
            ui.button('Delete service', on_click=_delete_service).props('color=red')
            ui.button('Test email', on_click=_test_email).classes('bg-purple-600 text-white')
            ui.button('⬇ Import from opencode', on_click=_import_opencode).classes('bg-teal-600 text-white')
        ui.button('Close', on_click=dlg.close).props('flat')
    _refresh()
    dlg.open()


# ============================================================
# START HARMONY
# ============================================================

ui.run(
    port=8080,
    title='Harmony AI aichat',
    show=False,
    reload=False,
)
