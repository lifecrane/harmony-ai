"""
harmony_model_center.py — Models Control Center (3 cards in 1 expansion).

Donor: niceai.py:17326 expansion
  'Models Control Center - Launch & Logs | Manage global runtime engines...'
  Row of 3 cards: Left 🧠 Local Default Model / Right 📥 HF Downloader /
  Middle 🔑 Credentials Vault.

Thin module rule: ALL UI + logic lives here. harmony-ai.py keeps only
`import harmony_model_center` + one call `build_bottom_bar()` (plus
`apply_theme()` for the dark theme / button shapes / colors).
Engine calls go through injected `mc` (aichat_model_controller) + `vault`
(auth_store). Nothing imported at boot that can fail.
"""
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

from nicegui import ui

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BENCH_FILE = os.path.join(BASE_DIR, 'Models', 'benched_models.txt')
BACKEND_FILE = os.path.join(BASE_DIR, 'Models', 'coder_backend.txt')
_KEY_TESTER_PORT = 8088
_KEY_TESTER_SCRIPT = os.path.join(BASE_DIR, 'test_ai_keys_remote.py')

_BACKENDS = ('kilo', 'opencode', 'aider')

_mc = None
_vault = None
_notify = None
_safe_update = None
_app_state = None
_base_dir = BASE_DIR
_refresh_model_status = None
_refresh_info = None
_save_settings = None
_HAS_MC = False
_HAS_VAULT = False


def inject_harmony_deps(deps: dict):
    """Inject harmony-ai deps once at boot. Never raises."""
    global _mc, _vault, _notify, _safe_update, _app_state, _base_dir
    global _refresh_model_status, _refresh_info, _save_settings
    global _HAS_MC, _HAS_VAULT
    try:
        _mc = deps.get('mc')
        _vault = deps.get('vault')
        _notify = deps.get('safe_notify')
        _safe_update = deps.get('safe_update')
        _app_state = deps.get('app_state')
        _base_dir = str(deps.get('base_dir') or BASE_DIR)
        _refresh_model_status = deps.get('refresh_model_status')
        _refresh_info = deps.get('refresh_info')
        _save_settings = deps.get('save_settings')
        _HAS_MC = bool(deps.get('has_mc', _mc is not None))
        _HAS_VAULT = bool(deps.get('has_vault', _vault is not None))
    except Exception:
        pass


def apply_theme():
    """Dark theme, Quasar color palette and button shapes from niceai.py.

    Kept in this module so harmony-ai.py stays slim; call once at boot.
    Never raises.
    """
    try:
        ui.dark_mode().enable()
        ui.colors(primary='#a41313', secondary='#046a2a', accent='#c89e1c')
        ui.add_head_html('''
<style>
html, body { background: #0a0a0a !important; }
.q-card { background: #161616 !important; border: 1px solid #2a2a2a; }
.q-expansion-item { background: #161616; border-radius: 14px; overflow: hidden; border: 1px solid #2a2a2a; }
.q-btn { border-radius: 10px !important; text-transform: none !important; font-weight: 600; }
.q-btn .q-btn__content { padding-inline: 4px; }
.q-btn { padding-inline: 6px; }
.hf-result { font-size: 1.05rem !important; font-weight: 600 !important; color: #d25cff !important; }
.hf-result:hover { color: #e89cff !important; }
/* Bottom Models Control Center — same slate blue-gray as the top chat panel
   (bg-gray-800 / border-gray-700) so top and bottom match. */
.q-expansion-item.datacard-control {
    background: #1f2937 !important;
    border: 1px solid #374151 !important;
}
/* Chat expansion v4 — same rounded card as bottom bar (datacard-control).
   Outer IS the blue card: bg + border + 14px corners + padding, header inside
   it like the bottom panel. Inner is transparent flex column filling height
   (dynamic size), so no double card / no empty band. */
.q-expansion-item.datacard-chat-outer {
    background: #1f2937 !important;
    border: 1px solid #374151 !important;
    border-radius: 14px !important;
    overflow: hidden !important;
    display: flex !important;
    flex-direction: column !important;
    flex: 1 1 auto !important;
    min-height: 0 !important;
    height: auto !important;
    box-shadow: 0 10px 15px -3px rgba(0,0,0,.5) !important;
}
.q-expansion-item.datacard-chat-outer > .q-expansion-item__container { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; }
/* Slim header bar — Quasar item defaults to ~48px tall, felt way too thick. */
.q-expansion-item.datacard-chat-outer .q-item { padding-top: 2px !important; padding-bottom: 2px !important; min-height: 32px !important; }
body .datacard-chat-outer .q-expansion-item__content { display: flex; flex-direction: column; min-height: 0; flex: 1 1 auto; }
body .datacard-chat-outer .q-expansion-item__content .nicegui-content {
    flex: 1 1 auto; display: flex; flex-direction: column;
    gap: 12px; background: transparent !important;
    border: none !important; padding: 8px 16px 8px 16px !important;
    max-height: none !important; height: auto !important;
    min-height: 0 !important; overflow: visible !important;
}
/* Dynamic like bottom: chat grows to 62vh then scrolls inside.
   Custom grab bar (#chat-resize-grab) below does the drag — native CSS
   resize does not survive Quasar's inner overflow wrappers. */
body .datacard-chat-outer .nicegui-content .q-scrollarea { flex: 0 1 auto !important; min-height: 34vh !important; max-height: 85vh !important; overflow-x: hidden !important; }
body .datacard-chat-outer .q-scrollarea .q-scrollarea__bar--h { display: none !important; }
#chat-resize-grab { height: 6px !important; cursor: ns-resize !important; border-radius: 3px !important; background: #374151 !important; opacity: .55 !important; margin: 0 !important; touch-action: none !important; }
#chat-resize-grab:hover { opacity: 1 !important; background: #4b5563 !important; }
/* Chat scroll fills the tall blue box (flex reference restored by h-screen
   outer); input pinned bottom. min-height:0 lets flex decide, no fixed vh. */
body .datacard-chat-outer .nicegui-content .q-scrollarea { flex: 1 1 auto !important; min-height: 0 !important; }
.q-card.datacard-localmodel,
.q-card.datacard-hf,
.q-card.datacard-vault {
    background: #1f2937 !important;
    border: 1px solid #374151 !important;
}
</style>
''')
    except Exception as ex:
        logging.warning('apply_theme failed: %s', ex)


def _say(msg, type='positive', timeout=8000):
    try:
        if _notify:
            _notify(msg, type=type, timeout=timeout)
        else:
            ui.notify(msg, type=type, timeout=timeout)
    except Exception:
        pass


def _upd(el):
    try:
        if _safe_update:
            _safe_update(el)
        else:
            el.update()
    except Exception:
        pass


def _mc_guard():
    if not _HAS_MC or _mc is None:
        ui.notify('Model controller module missing', type='negative')
        return False
    return True


def _clean(v):
    """Strip [LCL]/[CLD] prefixes + trailing ' (gguf-only)'. Never raises."""
    try:
        s = re.sub(r'^\[(LCL|CLD)\]\s*', '', str(v))
        return s.replace(' (gguf-only)', '').strip()
    except Exception:
        return v or ''


# ------------------------------------------------------------------
# bench + backend (logic, no UI)
# ------------------------------------------------------------------

def read_benched_models():
    """Set of benched (lowercased) model ids. Empty when none. Never raises."""
    try:
        if not os.path.isfile(BENCH_FILE):
            return set()
        with open(BENCH_FILE, 'r', encoding='utf-8') as fh:
            return {l.strip().lower() for l in fh.read().splitlines()
                    if l.strip() and not l.strip().startswith('#')}
    except Exception:
        return set()


def write_benched_models(bench):
    """Persist the bench set. Never raises."""
    try:
        os.makedirs(os.path.dirname(BENCH_FILE), exist_ok=True)
        with open(BENCH_FILE, 'w', encoding='utf-8') as fh:
            fh.write('# Benched team members — removed from dropdowns + failover chains.\n')
            fh.write('# Added by Team Check; uncheck to re-invite a member.\n')
            fh.write('\n'.join(sorted(bench)) + ('\n' if bench else ''))
        return True
    except Exception:
        return False


def is_benched(model_name):
    """True when full id or bare name (before ':') is benched. Never raises."""
    try:
        bench = read_benched_models()
        if not bench:
            return False
        m = (model_name or '').lower()
        bare = m.split(':')[0].split('@')[0].strip()
        return m in bench or bare in bench
    except Exception:
        return False


def unbench(model_name):
    """Remove one model from the bench. Returns True when changed."""
    try:
        bench = read_benched_models()
        m = (model_name or '').lower()
        bare = m.split(':')[0].split('@')[0].strip()
        before = set(bench)
        bench.discard(m)
        bench.discard(bare)
        if bench != before:
            write_benched_models(bench)
            return True
        return False
    except Exception:
        return False


def filter_benched(names):
    """Drop benched entries from a model name list. Never raises."""
    try:
        return [n for n in (names or []) if not is_benched(n)]
    except Exception:
        return list(names or [])


def get_backend():
    """Coder backend id (kilo/opencode/aider). Defaults to kilo."""
    try:
        if os.path.isfile(BACKEND_FILE):
            with open(BACKEND_FILE, 'r', encoding='utf-8') as fh:
                v = (fh.read() or '').strip().lower()
                if v in _BACKENDS:
                    return v
    except Exception:
        pass
    return 'kilo'


def set_backend(name):
    """Persist the coder backend. Returns True on success."""
    try:
        v = (name or '').strip().lower()
        if v not in _BACKENDS:
            return False
        os.makedirs(os.path.dirname(BACKEND_FILE), exist_ok=True)
        with open(BACKEND_FILE, 'w', encoding='utf-8') as fh:
            fh.write(v + '\n')
        return True
    except Exception:
        return False


def backend_options():
    """Backend ids for a dropdown. Never raises."""
    return list(_BACKENDS)


def run_team_check(model_names, on_line=None, on_done=None):
    """Ping each local model in a daemon thread; bench the failures."""
    names = [n for n in (model_names or []) if n and not n.startswith('(none')]

    def _emit(s):
        try:
            if on_line:
                on_line(s)
        except Exception:
            pass

    def _job():
        passed, failed = [], []
        try:
            if not _mc_guard():
                try:
                    if on_done:
                        on_done({'passed': [], 'benched': []})
                except Exception:
                    pass
                return
            for n in names:
                _emit(f'probing {n}...')
                try:
                    ok, _rep = _mc.ping_model(n)
                except Exception:
                    ok = False
                (passed if ok else failed).append(n)
            if failed:
                try:
                    bench = read_benched_models()
                    for n in failed:
                        bench.add(n.lower())
                        bench.add(n.lower().split(':')[0])
                    write_benched_models(bench)
                except Exception:
                    pass
                _say(f"Team Check: {len(passed)} passed, "
                     f"{len(failed)} benched ({', '.join(failed)})",
                     type='warning', timeout=10000)
            else:
                _say(f"Team Check: all {len(passed)} passed — bench empty",
                     type='positive', timeout=8000)
        except Exception as ex:
            _emit(f'Team Check error: {ex}')
            failed = list(names)
        try:
            if on_done:
                on_done({'passed': passed, 'benched': failed})
        except Exception:
            pass

    threading.Thread(target=_job, daemon=True).start()


# ------------------------------------------------------------------
# Collapsible bottom bar — donor layout: niceai.py:17326 expansion
# ------------------------------------------------------------------

def build_bottom_bar():
    """Render the collapsible 3-card Models Control Center bar inline."""
    if not _mc_guard():
        return
    try:
        keep = bool((_app_state or {}).get('keep_chat_logs', True))
    except Exception:
        keep = True

    def _toggle_logs(e):
        try:
            _app_state['keep_chat_logs'] = bool(e.value)
            if _save_settings:
                _save_settings()
        except Exception:
            pass

    with ui.expansion(
        '⚙️ Models Control Center - Launch & Logs | Manage global '
        'runtime engines, quantization routing, and live process '
        'diagnostics below.',
        icon='dashboard',
        value=True,
    ).classes('w-full datacard-control mt-2'):
        ui.checkbox('Keep chat logs', value=keep,
                    on_change=_toggle_logs).props('dark').classes(
            'text-xs text-emerald-400 font-semibold').tooltip(
            'Append every turn to History/chat.log')
        # 3 cards in a row, fully expanded (no internal scrollbar) — the page
        # grows and scrolls instead, matching niceai's bottom bar.
        with ui.row().classes('w-full gap-4 items-stretch flex-wrap'):
            with ui.column().classes('flex-1 gap-4 min-w-72'):
                _local_card()
            with ui.column().classes('flex-1 gap-4 min-w-72'):
                _hf_card()
            with ui.column().classes('flex-1 gap-4 min-w-72'):
                _vault_card()


def _local_card():
    """Left card: 🧠 Local Default Model (select + bench + cloud + backend)."""
    with ui.card().classes('w-full p-4 gap-3 datacard-localmodel'):
        ui.label('🧠 Local Default Model').classes(
            'text-lg font-semibold text-green-500')
        ui.label('Sets default model & orchestrator').classes(
            'text-xs text-gray-400 italic')
        with ui.column().classes('w-full gap-1'):
            loaded_lbl = ui.label('').classes('text-xs text-yellow-400 font-mono')
            ping_lbl = ui.label('').classes('text-xs text-sky-300 font-mono')
            prog_lbl = ui.label('').classes('text-xs font-mono text-yellow-300')
            prog_bar = ui.linear_progress(show_value=False).classes('w-full')
            prog_bar.visible = False
            sel = ui.select([], label='Choose Ollama Model FOR UNIVERSAL CHAT'
                            ).classes('w-full')
            tag_row = ui.row().classes('gap-1 flex-wrap')

            with ui.row().classes('gap-2 w-full items-center'):
                ui.label('Coder:').classes('text-xs text-gray-400')
                try:
                    _be_init = get_backend()
                except Exception:
                    _be_init = 'kilo'
                backend_sel = ui.select(backend_options(), value=_be_init,
                                        label='Backend').classes('flex-1')

                def _backend_changed(e):
                    try:
                        if set_backend(e.value):
                            _say(f'Coder backend -> {e.value} '
                                 '(takes effect on next terminal launch)')
                        else:
                            ui.notify('Backend save failed', type='warning')
                    except Exception:
                        pass
                try:
                    backend_sel.on_value_change(_backend_changed)
                except Exception:
                    pass

            # --- Benched members (unban by unchecking) ---
            bench_label = ui.label('').classes(
                'text-xs font-semibold text-red-400 mt-2')
            bench_box = ui.column().classes('w-full gap-0')

            def _refresh_bench_ui():
                bench_box.clear()
                benched = read_benched_models()
                bench_label.set_text(f'🛑 Benched ({len(benched)}):')
                with bench_box:
                    for m in sorted(benched):
                        def _mk_unbench(model):
                            def _flip(e):
                                if not e.value:
                                    unbench(model)
                                    _say(f'☀️ Unbenched {model} — back on the team!',
                                         type='positive')
                                    _refresh_bench_ui()
                                    _refresh()
                            return _flip
                        ui.checkbox(m, value=True,
                                    on_change=_mk_unbench(m)).props(
                            'dense').classes('text-xs')
                    if not benched:
                        ui.label('No benched members — the whole team is fit.'
                                 ).classes('text-xs text-gray-400 italic')

            ui.label('☁️ Cloud (vault key + lock — rest untouched)').classes(
                'text-xs text-purple-300 font-bold mt-1')
            cloud_lbl = ui.label('').classes('text-[11px] text-purple-200 font-mono')
            with ui.row().classes('gap-2 w-full'):
                prov_in = ui.input('Provider', value='openrouter').classes('flex-1')
                url_in = ui.input('Base URL (blank = known default)',
                                  placeholder='https://openrouter.ai/api/v1'
                                  ).classes('flex-2')
            cloud_in = ui.input('Cloud model id (e.g. liquid/lfm-2.5-2.6b:free)'
                                ).classes('w-full')
            try:
                _pref_path = os.path.join(str(_base_dir), 'Models',
                                          'preferred_cloud.txt')
                with open(_pref_path, 'r', encoding='utf-8') as _pf:
                    _pref_lines = [l.strip() for l in _pf.read().splitlines()
                                   if l.strip()]
                if _pref_lines:
                    cloud_in.value = _pref_lines[0]
            except Exception:
                pass
            _KNOWN_CLOUD_BASE = {
                'openrouter': 'https://openrouter.ai/api/v1',
                'deepseek': 'https://api.deepseek.com/v1',
            }
            ui.html(
                'No key yet? Get a free one at <b>openrouter.ai/keys</b>, '
                'then add it in <b>Vault</b> (service <b>openrouter</b>).'
            ).classes('text-[11px] text-purple-300/70 leading-snug')

            def _cloud_status():
                try:
                    active = _mc.get_aichat_active_model()
                except Exception:
                    active = ''
                try:
                    cloud_lbl.text = f"active chat model: {active or '(unknown)'}"
                except Exception:
                    pass

            def _use_cloud():
                prov = (prov_in.value or '').strip().lower() or 'openrouter'
                mid = (cloud_in.value or '').strip()
                if mid.lower().startswith(prov + ':'):
                    mid = mid[len(prov) + 1:].strip()
                if not mid:
                    ui.notify('Type a cloud model id first', type='warning')
                    return
                base = ((url_in.value or '').strip()
                        or _KNOWN_CLOUD_BASE.get(prov, ''))
                if not base:
                    ui.notify('Unknown provider — paste its Base URL too',
                              type='warning')
                    return
                if not _HAS_VAULT or _vault is None:
                    ui.notify('Vault module missing', type='negative')
                    return
                try:
                    unlocked = _vault.is_unlocked()
                except Exception:
                    unlocked = False
                if not unlocked:
                    ui.notify('Unlock the Vault first (Vault card: passphrase)',
                              type='warning')
                    return
                try:
                    key = _vault.get(prov, 'api_key')
                except Exception as ex:
                    ui.notify(f'Vault read failed: {ex}', type='negative')
                    return
                if not key:
                    ui.notify(f'No key for {prov} in Vault — Vault card → '
                              'Add service', type='warning')
                    return
                ok, msg = _mc.set_aichat_cloud_model(prov, mid, key, base)
                _say(msg, type='positive' if ok else 'negative', timeout=8000)
                _cloud_status()

            def _back_to_local():
                name = sel.value
                if not name or name.startswith('(none'):
                    ui.notify('Pick a local model first', type='warning')
                    return
                if '(gguf-only)' in name:
                    ui.notify('Import the GGUF first', type='warning')
                    return
                try:
                    ok, msg = _mc.set_aichat_model(name)
                except Exception as ex:
                    ui.notify(f'config update failed: {ex}', type='negative')
                    return
                _say(msg, type='positive' if ok else 'warning', timeout=8000)
                _cloud_status()

            def _show_progress(text=''):
                try:
                    prog_bar.visible = True
                    if text:
                        prog_lbl.text = text
                    _upd(prog_lbl)
                    _upd(prog_bar)
                except Exception:
                    pass

            def _hide_progress():
                try:
                    prog_bar.visible = False
                    prog_lbl.text = ''
                    _upd(prog_lbl)
                    _upd(prog_bar)
                except Exception:
                    pass

            gguf_paths = {}

            def _render_tag_chips():
                tag_row.clear()
                try:
                    import local_tags as _lt
                except Exception:
                    return
                cur = sel.value
                if not cur or cur.startswith('(none'):
                    return
                clean = _clean(cur)
                if '(gguf-only)' in (cur or ''):
                    return
                cur_tags = _lt.get_tags(clean)
                with tag_row:
                    ui.label('tags:').classes('text-[10px] text-gray-500 self-center')
                    for _t in _lt.ALLOWED_TAGS:
                        _on = _t in cur_tags
                        ui.button(
                            f"✓ {_t}" if _on else _t,
                            on_click=lambda t=_t: (_lt.toggle_tag(clean, t),
                                                  _refresh()),
                        ).props(('unelevated dense size=xs color=positive'
                                 if _on else 'outline dense size=xs color=grey')
                                ).classes('text-[10px]').tooltip(
                            f"{'Remove' if _on else 'Add'} '{_t}' tag")

            def _refresh():
                def _sort_opt(lbl):
                    try:
                        import local_tags as _lt
                        return _lt.sort_key_for_label(lbl)
                    except Exception:
                        return (1, (lbl or '').lower())
                models = _mc.ollama_models()
                tags = [m['name'] for m in models]
                benched = read_benched_models()
                if benched:
                    tags = [t for t in tags
                            if t.lower() not in benched
                            and t.lower().split(':')[0] not in benched]
                g = _mc.scan_gguf_folder(str(_base_dir))
                gguf_paths.clear()
                for n, p in g.items():
                    if not _mc.gguf_is_imported(n, models):
                        gguf_paths[n] = p
                opts = sorted((_mc.tag_local(t) for t in tags), key=_sort_opt)
                opts += sorted((_mc.tag_local(f'{n} (gguf-only)')
                                for n in gguf_paths), key=_sort_opt)

                def _key(v):
                    return _clean(v)
                _keep_clean = _key(sel.value) if sel.value else None
                sel.set_options(opts or ['(none — is Ollama running?)'])
                if _keep_clean:
                    for _o in opts:
                        if _key(_o) == _keep_clean:
                            sel.value = _o
                            break
                sel.update()
                _render_tag_chips()
                _refresh_bench_ui()
                _cloud_status()
                try:
                    loaded = _mc.loaded_names()
                    loaded_lbl.text = (f"in RAM: {', '.join(loaded)}"
                                       if loaded else 'in RAM: (none)')
                except Exception:
                    pass
                ui.notify('Model list refreshed')

            def _gguf_key_from_value(v):
                if not v or '(gguf-only)' not in v:
                    return None
                return _clean(v).replace('[LCL] ', '').strip()

            def _prompt_import(gguf_path):
                stem = Path(gguf_path).stem
                suggested = (re.sub(r'[^a-z0-9]+', '-', stem.lower()).strip('-')
                             or 'imported-model')
                with ui.dialog() as ndlg, ui.card().classes(
                        'p-4 gap-2 min-w-80 dialog-drag'):
                    ui.label(f'Import {Path(gguf_path).name}').classes(
                        'font-bold text-emerald-400 drag-handle')
                    ui.label('No Ollama entry yet — give it a name:').classes(
                        'text-xs text-gray-400')
                    name_in = ui.input('Model name', value=suggested).classes('w-full')
                    ui.label(f'GGUF: {gguf_path}').classes(
                        'text-[10px] font-mono text-gray-500 break-all')

                    def _go():
                        mname = name_in.value.strip() or suggested
                        ndlg.close()
                        try:
                            _app_state['loading_model'] = mname
                        except Exception:
                            pass
                        if _refresh_model_status:
                            try:
                                _refresh_model_status()
                            except Exception:
                                pass

                        def _run():
                            _show_progress(f'importing {mname}...')

                            def _line(txt):
                                _show_progress(txt)

                            ok = _mc.import_single_model(mname, gguf_path,
                                                         on_line=_line)
                            if ok:
                                _show_progress(f'imported {mname} — loading...')
                                _mc.load_model(mname, timeout=900,
                                               base_dir=str(_base_dir))
                                try:
                                    _app_state['loading_model'] = None
                                except Exception:
                                    pass
                                if _refresh_model_status:
                                    try:
                                        _refresh_model_status()
                                    except Exception:
                                        pass
                                _hide_progress()
                                try:
                                    _mc.set_aichat_model(mname)
                                except Exception:
                                    pass
                            else:
                                try:
                                    _app_state['loading_model'] = None
                                except Exception:
                                    pass
                                if _refresh_model_status:
                                    try:
                                        _refresh_model_status()
                                    except Exception:
                                        pass
                                _hide_progress()
                                _say(f'Import failed for {mname}',
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

                    ui.button('Import + Load', icon='download',
                              on_click=_go).props('unelevated color=primary size=sm')
                    ui.button('Cancel', on_click=ndlg.close).props('flat size=sm')
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
                ui.notify(f'Loading {name} — slow models take minutes...',
                          type='info')
                try:
                    _app_state['loading_model'] = name
                except Exception:
                    pass
                if _refresh_model_status:
                    try:
                        _refresh_model_status()
                    except Exception:
                        pass

                def _go():
                    _show_progress(f'loading {name}...')
                    ok, msg = _mc.load_model(name, timeout=900,
                                             base_dir=str(_base_dir))
                    try:
                        _app_state['loading_model'] = None
                    except Exception:
                        pass
                    if _refresh_model_status:
                        try:
                            _refresh_model_status()
                        except Exception:
                            pass
                    _hide_progress()
                    _say(msg, type='positive' if ok else 'negative',
                         timeout=10000)
                    try:
                        ok2, msg2 = _mc.set_aichat_model(name)
                        _say(msg2, type='positive' if ok2 else 'warning',
                             timeout=8000)
                    except Exception as ex:
                        _say(f'aichat config update failed: {ex}',
                             type='warning', timeout=8000)
                    try:
                        loaded_lbl.text = (
                            f"in RAM: {', '.join(_mc.loaded_names()) or '(none)'}")
                    except Exception:
                        pass
                threading.Thread(target=_go, daemon=True).start()

            def _ping():
                name = sel.value
                if not name or name.startswith('(none'):
                    ui.notify('Pick a model first', type='warning')
                    return
                try:
                    ping_lbl.text = f'pinging {name} (real workload)...'
                except Exception:
                    pass

                def _go():
                    ok, rep = _mc.ping_model(name)
                    if ok:
                        try:
                            _mc.log_ping(rep, str(_base_dir))
                        except Exception:
                            pass
                        ttft = (f"{rep['ttft']:.1f}s" if rep.get('ttft')
                                else 'n/a')
                        state = '⚡ warm' if rep.get('warm') else '⏳ cold'
                        try:
                            ping_lbl.text = (
                                f"{state} {rep['model']} "
                                f"[{rep.get('family', '?')}] — "
                                f"first token {ttft} · total "
                                f"{rep['total']:.1f}s")
                        except Exception:
                            pass
                        ui.notify(
                            f"{state} {rep['model']} first token {ttft}, "
                            f"total {rep['total']:.1f}s",
                            type='positive', timeout=8000)
                        try:
                            _app_state['last_ping'] = rep
                            if _refresh_info:
                                _refresh_info()
                        except Exception:
                            pass
                    else:
                        try:
                            ping_lbl.text = (
                                f"ping failed: {rep.get('error', '?')}")
                        except Exception:
                            pass
                        ui.notify(f"Ping failed: {rep.get('error', '?')}",
                                  type='negative', timeout=8000)
                threading.Thread(target=_go, daemon=True).start()

            def _unload():
                name = sel.value
                if not name or name.startswith('(none'):
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
                try:
                    loaded_lbl.text = (
                        f"in RAM: {', '.join(_mc.loaded_names()) or '(none)'}")
                except Exception:
                    pass
                ui.notify(f'Unloaded {name}')

            def _tune():
                name = sel.value
                if not name or name.startswith('(none'):
                    ui.notify('Pick a model first', type='warning')
                    return
                cur = _mc.get_tuning(str(_base_dir), name)
                with ui.dialog() as tdlg, ui.card().classes(
                        'p-4 gap-2 min-w-72 dialog-drag'):
                    ui.label(f'Tune {name}').classes(
                        'font-bold text-emerald-400 drag-handle')
                    ctx_in = ui.input('num_ctx',
                                      value=str(cur.get('num_ctx', ''))
                                      ).classes('w-full')
                    thr_in = ui.input('num_thread',
                                      value=str(cur.get('num_thread', ''))
                                      ).classes('w-full')
                    tmp_in = ui.input('temperature',
                                      value=str(cur.get('temperature', ''))
                                      ).classes('w-full')

                    def _save():
                        def _num(v):
                            try:
                                return (float(v) if v not in (None, '')
                                        else None)
                            except Exception:
                                return None
                        ok = _mc.set_tuning(str(_base_dir), name, {
                            'num_ctx': _num(ctx_in.value),
                            'num_thread': _num(thr_in.value),
                            'temperature': _num(tmp_in.value)})
                        ui.notify('Tuning saved (applies at next load)'
                                  if ok else 'Save failed',
                                  type='positive' if ok else 'negative')
                        tdlg.close()
                    ui.button('Save', on_click=_save).props(
                        'unelevated color=primary size=sm')
                tdlg.open()

            def _delete_traces():
                name = sel.value
                if not name or name.startswith('(none'):
                    ui.notify('Pick a model first', type='warning')
                    return
                clean = _clean(name)
                with ui.dialog() as cdlg, ui.card().classes(
                        'p-4 gap-2 dialog-drag'):
                    ui.label(f'🗑️ Delete ALL traces of "{clean}"?').classes(
                        'font-bold text-red-400 drag-handle')
                    ui.label('Removes: Ollama entry + blobs, GGUF file, '
                             'Modelfile, tuning profile. This frees disk space '
                             'and cannot be undone.').classes('text-xs text-gray-400')
                    with ui.row().classes('gap-2'):
                        ui.button('Yes, delete everything', on_click=lambda: (
                            cdlg.close(), _do_delete(name))).props(
                            'unelevated color=red-6')
                        ui.button('Cancel', on_click=cdlg.close).props('outline')
                cdlg.open()

            def _do_delete(name):
                try:
                    ok, msg = _mc.delete_model_all_traces(name, str(_base_dir))
                    _say(msg, type='positive' if ok else 'negative')
                    _refresh()
                except Exception as ex:
                    _say(f'Delete error: {ex}', type='negative')

            with ui.row().classes('gap-2 flex-wrap'):
                ui.button('Use cloud ☁️', on_click=_use_cloud).props(
                    'unelevated color=deep-purple-4 size=sm')
                ui.button('Back to local', on_click=_back_to_local).props(
                    'outline color=grey size=sm')

            def _team_check():
                names = []
                try:
                    for m in (_mc.ollama_models() or []):
                        n = (m.get('name') or '').strip()
                        if n and not is_benched(n):
                            names.append(n)
                except Exception:
                    pass
                if not names:
                    ui.notify('No local models to probe', type='warning')
                    return
                try:
                    prog_lbl.text = (f'Team Check: probing {len(names)} '
                                     'model(s)...')
                except Exception:
                    pass

                def _line(s):
                    try:
                        prog_lbl.text = f'Team Check: {s}'
                    except Exception:
                        pass

                def _done(rep):
                    try:
                        prog_lbl.text = (
                            f"Team Check done: "
                            f"{len(rep.get('passed', []))} passed, "
                            f"{len(rep.get('benched', []))} benched")
                    except Exception:
                        pass
                    try:
                        _refresh()
                    except Exception:
                        pass
                run_team_check(names, on_line=_line, on_done=_done)
                ui.notify(f'Team Check started on {len(names)} model(s) — '
                          'failures get benched', type='info')

            def _unbench_all():
                try:
                    write_benched_models(set())
                    ui.notify('Bench cleared — refresh to see all models',
                              type='positive')
                    _refresh()
                except Exception:
                    pass

            with ui.row().classes('gap-2 flex-wrap'):
                ui.button('Refresh Models', icon='refresh',
                          on_click=_refresh).props('outline color=accent size=sm')
                ui.button('Tune', icon='tune', on_click=_tune).props(
                    'outline color=purple-6 size=sm').tooltip(
                    'Per-model flags: sampling, engine, raw JSON')
                ui.button('Load & Launch', on_click=_load_launch).props(
                    'unelevated color=primary size=sm')
                ui.button('🗑️ Delete All Traces', icon='delete',
                          on_click=_delete_traces).props(
                    'outline color=red-6 size=sm').tooltip(
                    'Unload from Ollama + delete blobs/GGUF/tuning — frees disk')
                ui.button('⚡ Ping', icon='bolt', on_click=_ping).props(
                    'outline color=lime-6 size=sm')
                ui.button('Unload', on_click=_unload).props(
                    'outline color=grey size=sm')
                ui.button('🧑‍🤝‍🧑 Team Check', icon='group',
                          on_click=_team_check).props(
                    'outline color=teal-6 size=sm').tooltip(
                    'Probe every local model — failures go to the bench and '
                    'hide from the list')
                ui.button('Unbench all', on_click=_unbench_all).props(
                    'flat size=sm').tooltip('Clear the bench list')
            try:
                sel.on_value_change(lambda e: _render_tag_chips())
            except Exception:
                pass
            _refresh()


def _hf_card():
    """Right card: 📥 Hugging Face Downloader (search → repo → file → dl)."""
    with ui.card().classes('w-full p-4 gap-3 datacard-hf'):
        ui.label('📥 Hugging Face Downloader').classes(
            'text-lg font-semibold text-blue-400')
        ui.label('Type a provider (e.g. google) → Search → click a model. '
                 'No guessing names.').classes('text-xs text-gray-400')
        search_row = ui.row().classes('w-full gap-2 items-end')
        with search_row:
            org_in = ui.input(label='Provider / Org (e.g. google)').classes(
                'flex-grow text-white')
            q_in = ui.input(label='Search (optional, e.g. gemma)').classes(
                'flex-grow text-white')
        hf_results = ui.column().classes('w-full gap-1 items-start').style(
            'max-height: 140px; overflow: auto;')
        hf_repo_input = ui.input(
            label='HF Repository ID (auto-filled, still editable)').classes(
            'w-full text-white')
        hf_file_select = ui.select([], label='GGUF file (auto-filled)').classes(
            'w-full')
        hf_size_lbl = ui.label('').classes('text-xs text-sky-300 font-mono')
        hf_progress = ui.linear_progress(value=0).classes('w-full mt-2')
        hf_progress.set_visibility(False)
        _hf_sizes = {}

        def _on_file_change(e):
            size = _hf_sizes.get(e.value or '', 0)
            try:
                hf_size_lbl.text = (
                    f'Size: {size / 1024 / 1024 / 1024:.2f} GB'
                    if size else '')
            except Exception:
                pass
        hf_file_select.on_value_change(_on_file_change)

        def _disk_label_text():
            try:
                du = shutil.disk_usage(str(_base_dir))
                return f'💾 Free disk: {du.free / 1024**3:.1f} GB'
            except Exception:
                return '💾 Free disk: ?'
        ui.label(_disk_label_text()).classes('text-xs text-gray-400')
        auto_imp = ui.switch('🗑️ Auto-import to Ollama + delete GGUF after download',
                             value=True).props('dense color=primary').tooltip(
            'Imports into Ollama, then removes the .gguf. Uncheck to just download.'
        )

        def _search_click():
            org = org_in.value.strip()
            q = q_in.value.strip()
            hf_results.clear()
            with hf_results:
                ui.label('Searching HuggingFace...').classes(
                    'text-xs text-gray-400')

            def _run():
                try:
                    results = _mc.search_hf_models(org, q)
                except Exception as ex:
                    results = [f'Search failed: {ex}']
                hf_results.clear()
                with hf_results:
                    if not results:
                        ui.label('No results.').classes('text-xs text-red-400')
                        return
                    first = results[0][0] if isinstance(results[0], (list, tuple)) else results[0]
                    if str(first).startswith(('No models', 'Search failed')):
                        ui.label(str(first)).classes('text-xs text-red-400')
                        return
                    for repo_id, dl in results:
                        _model_only = repo_id.split('/', 1)[1] if '/' in repo_id else repo_id
                        ui.button(f'{_model_only}   ({dl:,} downloads)',
                                  on_click=lambda r=repo_id: _pick_repo(r)
                                  ).props('flat dense size=sm no-wrap').classes(
                            'justify-start hf-result')
                ui.notify(f'{len(results)} result(s)')
            threading.Thread(target=_run, daemon=True).start()

        def _pick_repo(repo_id):
            hf_repo_input.value = repo_id
            hf_file_select.set_options([])
            hf_file_select.update()
            ui.notify(f'Loading files for {repo_id}...', type='info')

            def _run():
                try:
                    files = _mc.list_gguf_files(repo_id)
                except Exception as ex:
                    files = [f'File list failed: {ex}']
                if not files or (isinstance(files[0], str)
                                 and files[0].startswith('File list failed')):
                    ui.notify(files[0] if files else 'No GGUF files found',
                              type='negative')
                    return
                _hf_sizes.clear()
                for name, size in files:
                    _hf_sizes[name] = size
                # Plain list of filenames — displays exactly like before and
                # select.value returns the true filename (used by the download).
                hf_file_select.set_options([name for name, _ in files])
                hf_file_select.update()
                ui.notify(f'{len(files)} GGUF file(s) found — pick one and download',
                          type='positive')
            threading.Thread(target=_run, daemon=True).start()

        def _download():
            repo_id = hf_repo_input.value.strip()
            filename = hf_file_select.value
            if not repo_id or not filename:
                ui.notify('Search → click repo → pick file first', type='warning')
                return
            hf_progress.set_visibility(True)
            hf_progress.value = 0

            def _prog(p):
                try:
                    _app_state["dl"] = {
                        'pct': p,
                        'label': f'{repo_id}/{filename}',
                        'active': p < 100,
                    }
                except Exception:
                    pass
                try:
                    hf_progress.value = max(0.0, min(1.0, (p or 0) / 100.0))
                except Exception:
                    pass

            def _run():
                try:
                    ok, msg = _mc.download_hf_file(
                        repo_id, filename, str(_base_dir),
                        known_size_bytes=int(_hf_sizes.get(filename, 0)),
                        auto_import=auto_imp.value,
                        delete_after=auto_imp.value,
                        on_progress=_prog)
                except Exception as ex:
                    ok, msg = False, f'download failed: {ex}'
                try:
                    _app_state["dl"] = {
                        'pct': 100 if ok else 0,
                        'label': f'{repo_id}/{filename}',
                        'active': False,
                    }
                except Exception:
                    pass
                ui.notify(msg, type='positive' if ok else 'negative',
                          timeout=10000)
                try:
                    hf_progress.set_visibility(False)
                except Exception:
                    pass
            threading.Thread(target=_run, daemon=True).start()

        with search_row:
            ui.button('Search', icon='search', on_click=_search_click).props(
                'unelevated color=accent size=sm')

        ui.button('Download Selected File', icon='download',
                  on_click=_download).props('unelevated color=primary size=sm')


def _vault_card():
    """Middle card: 🔑 Credentials Vault (gpg-encrypted store)."""
    with ui.card().classes('w-full p-4 gap-3 datacard-vault'):
        ui.label('🔑 Credentials Vault').classes(
            'text-lg font-semibold text-purple-400')
        ui.label('gpg-encrypted store for API keys & email — nothing readable '
                 'on disk').classes('text-xs text-gray-400 italic')
        if not _HAS_VAULT or _vault is None:
            ui.label('auth_store.py missing').classes('text-xs text-red-400')
            return
        vault_status = ui.label('...').classes('text-sm font-mono text-yellow-400')
        vault_keep = ui.checkbox(
            'Keep unlocked while panel runs (email works when you are away)',
            value=True).props('dense')
        vault_keep.on_value_change(
            lambda e: _vault.set_idle_lock(None if e.value
                                           else _vault.IDLE_LOCK_SECONDS))
        vault_stay = ui.checkbox(
            'Stay unlocked ACROSS restarts (remember password until I clear it)',
            value=_vault.remembered()).props('dense')

        def _stay_change(e):
            if e.value:
                if not _vault.is_unlocked():
                    _say('Unlock the vault first, then tick this to remember '
                         'it across restarts.', type='warning')
                    e.sender.value = False
                    return
                _vault.remember_across_restarts(_vault.passphrase())
                _say('Password remembered — vault stays unlocked across '
                     'reboots until you clear it.', type='positive')
            else:
                _vault.clear_remembered()
                _say('Cleared the remembered password — next boot will ask '
                     'again.', type='info')
        vault_stay.on_value_change(_stay_change)
        vault_list = ui.column().classes('w-full gap-1')

        def _refresh():
            try:
                if not _vault.vault_exists():
                    vault_status.text = 'NO VAULT YET — click "Unlock" to create'
                    vault_unlock_btn.set_text('Create vault')
                    vault_list.clear()
                    with vault_list:
                        ui.label('One passphrase encrypts everything stored '
                                 'here. You type it once per session.').classes(
                            'text-xs text-gray-400')
                    return
                if _vault.is_unlocked():
                    vault_status.text = 'UNLOCKED — auto-locks when idle'
                    vault_unlock_btn.set_text('Re-lock')
                    vault_list.clear()
                    svcs = _vault.services()
                    if not svcs:
                        with vault_list:
                            ui.label('Empty — add a service.').classes(
                                'text-xs text-gray-400')
                    for name, fields in svcs:
                        with vault_list:
                            with ui.row().classes('w-full items-center gap-2'):
                                ui.label(name).classes(
                                    'text-sm font-mono text-purple-300 w-28 shrink-0')
                                ui.label(', '.join(f'{k}={v}' for k, v in fields.items())
                                         ).classes('text-xs font-mono text-gray-300 flex-grow')
                                ui.button(icon='delete',
                                          on_click=lambda n=name: _remove(n)).props(
                                    'flat dense size=sm color=red-6').tooltip('Remove')
                else:
                    vault_status.text = 'LOCKED'
                    vault_unlock_btn.set_text('Unlock')
                    vault_list.clear()
                    with vault_list:
                        ui.label('Unlock to view or edit stored credentials.'
                                 ).classes('text-xs text-gray-400')
            except Exception as e:
                try:
                    vault_status.text = f'vault error: {e}'
                except Exception:
                    pass

        def _unlock_dialog():
            if not _vault.vault_exists():
                _create_dialog()
                return
            with ui.dialog() as dlg, ui.card().classes('p-5 gap-3 w-80'):
                ui.label('🔑 Unlock Credentials Vault').classes(
                    'text-lg font-semibold')
                pw = ui.input(label='Passphrase', password=True,
                              password_toggle_button=True).classes('w-full')
                keep = ui.checkbox('Stay unlocked across restarts', value=True
                                   ).props('dense')

                def do_unlock():
                    ok, msg = _vault.unlock(pw.value)
                    if ok:
                        if keep.value:
                            _vault.remember_across_restarts(pw.value)
                        dlg.close()
                        _refresh()
                        _say('Vault unlocked' + (' — will stay unlocked across '
                              'restarts' if keep.value else ''), type='positive')
                    else:
                        _say(msg, type='negative')
                ui.button('Unlock', on_click=do_unlock).props(
                    'unelevated color=primary')
            dlg.open()
            pw.focus()

        def _create_dialog():
            with ui.dialog() as dlg, ui.card().classes('p-5 gap-3 w-96'):
                ui.label('🔑 Create Credentials Vault').classes(
                    'text-lg font-semibold')
                pw1 = ui.input(label='Passphrase', password=True,
                               password_toggle_button=True).classes('w-full')
                pw2 = ui.input(label='Confirm passphrase', password=True,
                               password_toggle_button=True).classes('w-full')
                or_key = ui.input(
                    label='OpenRouter API key (optional — stored encrypted)',
                    value=os.environ.get('OPENROUTER_API_KEY', ''),
                    password=True, password_toggle_button=True).classes('w-full')

                def do_create():
                    if pw1.value != pw2.value:
                        _say('Passphrases do not match', type='negative')
                        return
                    initial = {}
                    if or_key.value.strip():
                        initial['openrouter'] = {'api_key': or_key.value.strip()}
                    ok, msg = _vault.create_vault(pw1.value, initial)
                    if ok:
                        dlg.close()
                        _refresh()
                        _say('Vault created — credentials are now encrypted',
                             type='positive')
                    else:
                        _say(msg, type='negative')
                ui.button('Create vault', on_click=do_create).props(
                    'unelevated color=primary')
            dlg.open()
            pw1.focus()

        def _add_dialog():
            if not _vault.is_unlocked():
                _say('Unlock the vault first', type='warning')
                return
            with ui.dialog() as dlg, ui.card().classes('p-5 gap-3 w-96'):
                ui.label('➕ Add service / connector to vault').classes(
                    'text-lg font-semibold')
                name_in = ui.input('Service (e.g. deepseek)').classes('w-full')
                key_in = ui.input('API key', password=True,
                                  password_toggle_button=True).classes('w-full')
                url_in = ui.input('Base URL (optional)').classes('w-full')

                def _save():
                    ok, msg = _vault.set_credential(name_in.value.strip(),
                                                    'api_key', key_in.value)
                    if url_in.value.strip():
                        _vault.set_credential(name_in.value.strip(), 'base_url',
                                              url_in.value.strip())
                    _say(msg, type='positive' if ok else 'negative')
                    dlg.close()
                    _refresh()
                ui.button('Save service', on_click=_save).props(
                    'unelevated color=primary')
            dlg.open()

        def _remove(service):
            if not _vault.is_unlocked():
                _say('Unlock the vault first', type='warning')
                return
            ok, msg = _vault.remove_service(service)
            _say(msg, type='positive' if ok else 'negative')
            _refresh()

        def _lock_now():
            _vault.lock()
            _refresh()
            _say('Vault locked — wiped from memory', type='info')

        def _test_email():
            ok, msg = _vault.send_test_email()
            _say(msg, type='positive' if ok else 'negative')

        def _import_opencode():
            if not _vault.is_unlocked():
                _say('Unlock the vault first, then import.', type='warning')
                return
            oc_auth = Path.home() / '.local/share/opencode/auth.json'
            if not oc_auth.exists():
                _say(f'opencode auth not found: {oc_auth}', type='warning')
                return
            try:
                oc = json.loads(oc_auth.read_text())
            except Exception as e:
                _say(f'Could not read opencode auth: {e}', type='negative')
                return
            base_urls = {
                'deepseek': 'https://api.deepseek.com/v1',
                'openrouter': 'https://openrouter.ai/api/v1',
            }
            wanted = set(base_urls)
            added, skipped = [], []
            for prov, cfg in oc.items():
                if prov not in wanted:
                    continue
                key = (cfg or {}).get('key') if isinstance(cfg, dict) else None
                if not key:
                    continue
                if prov in [s for s, _ in _vault.services()]:
                    skipped.append(prov)
                    continue
                fields = {'api_key': key}
                if base_urls.get(prov):
                    fields['base_url'] = base_urls[prov]
                try:
                    for fld, val in fields.items():
                        _vault.set_credential(prov, fld, val)
                    added.append(prov)
                except Exception as e:
                    logging.warning('[vault-import] %s: %s', prov, e)
            _refresh()
            if added:
                _say(f'⬇️ Imported into vault: {", ".join(added)}'
                     + (f' (skipped existing: {", ".join(skipped)})'
                        if skipped else ''), type='positive', timeout=8000)
            elif skipped:
                _say(f'Already in vault: {", ".join(skipped)}', type='info')
            else:
                _say('No provider keys found in opencode auth.', type='warning')

        with ui.row().classes('gap-2 items-center'):
            vault_unlock_btn = ui.button('Unlock', icon='lock_open',
                                         on_click=_unlock_dialog).props(
                'unelevated color=primary size=sm')
            ui.button('Lock now', icon='lock', on_click=_lock_now).props(
                'outline color=warning size=sm')
        with ui.row().classes('gap-2 items-center flex-wrap'):
            ui.button('Add service', icon='add', on_click=_add_dialog).props(
                'outline color=accent size=sm')
            ui.button('Test email', icon='mail', on_click=_test_email).props(
                'outline color=green-6 size=sm')
            ui.button('Test all keys', icon='key', on_click=_open_key_tester).props(
                'outline color=teal-6 size=sm').tooltip(
                'Open the bulk key validator in a new tab — parses your Kilo '
                'config and tests every stored key in one go')
            ui.button('⬇ Import from opencode', icon='download',
                      on_click=_import_opencode).props(
                'outline color=deep-purple-4 size=sm').tooltip(
                'Copy the paid provider keys (deepseek/openrouter) from opencode '
                'into the encrypted vault')
        _refresh()


def _lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return 'localhost'


def _key_tester_up():
    try:
        s = socket.create_connection(('127.0.0.1', _KEY_TESTER_PORT), timeout=2)
        s.close()
        return True
    except Exception:
        return False


def _open_key_tester():
    """Launch the standalone 'Test all keys' app on :8088, open a new tab."""
    try:
        if not os.path.exists(_KEY_TESTER_SCRIPT):
            _say(f'Key tester script not found: {_KEY_TESTER_SCRIPT}',
                 type='negative')
            return
        if not _key_tester_up():
            subprocess.Popen(
                [sys.executable, _KEY_TESTER_SCRIPT],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                cwd=os.path.dirname(_KEY_TESTER_SCRIPT),
            )
        url = f'http://{_lan_ip()}:{_KEY_TESTER_PORT}'
        _say(f'Opening key tester at {url}', type='positive')
        ui.run_javascript(f'window.open({json.dumps(url)}, "_blank")')
    except Exception as ex:
        _say(f'key tester error: {ex}', type='negative')