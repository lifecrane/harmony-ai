"""aichat_model_controller.py — Models Control Center ENGINE for the aichat panel.

Slim backend for model/HF/vault work (no NiceGUI here). All UI lives in
harmony-ai.py; this module does disk + Ollama + HF work and reports via
return values / callbacks so buttons are thin.

Ported from: scan_gguf_folder, import_single_model, delete_model_all_traces,
load/save_model_tuning, search_hf_models, list_gguf_files, download_hf_file,
ensure_ollama_running (+ load/unload via keep_alive).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time

# Serializes ALL loads: two concurrent Load & Launch clicks (or load + chat)
# used to interleave evict/load and land two models in RAM at once
# (lfm-rag 846MB + granite 3.4GB = OOM territory on this box).
_LOAD_LOCK = threading.Lock()

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

OLLAMA_URL = 'http://localhost:11434'


def _gguf_root(base_dir):
    return os.path.join(os.path.abspath(str(base_dir)), 'Models')


def _tuning_file(base_dir):
    return os.path.join(_gguf_root(base_dir), 'model_tuning.json')


# ---------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------

def normalize_model_name(filename: str, gguf_root=None) -> str:
    """'Ornith-1.5-9B-Q5_K_M.gguf' -> 'ornith-1.5-9b-q5' (keeps quant)."""
    raw = os.path.splitext(filename)[0]
    q_match = re.search(r'[-_.]?q\s*(\d[\w.]*(?:_[A-Z])?)\.?\s*$', raw, re.IGNORECASE)
    quant = q_match.group(1).lower() if q_match else ''
    if q_match:
        raw = raw[:q_match.start()]
        raw = re.sub(r'[-_.]+$', '', raw)
    raw = raw.lower().replace('_', '-')
    m = re.search(r'(\d+\.?\d*)\s*b', raw)
    if m:
        size = m.group(1) + 'b'
        prefix = raw[:m.start()].rstrip('-')
        model_name = f'{prefix}-{size}'
    else:
        model_name = raw
    if quant:
        model_name = f'{model_name}-q{quant}'
    model_name = re.sub(r'-{2,}', '-', model_name).rstrip('-')
    if gguf_root:
        try:
            with open(os.path.join(gguf_root, 'model_map.txt'), 'a') as logf:
                logf.write(f'{filename} -> {model_name}\n')
        except Exception:
            pass
    return model_name


def _clean(name):
    """Strip display chrome back to the clean ollama name.

    Handles: `[LCL]/[CLD]` prefix, ` (gguf-only)` suffix, and the
    ` · tag` usability suffixes from local_tags.py (label-only badges).
    """
    s = re.sub(r'^\[(LCL|CLD)\]\s*', '', name or '').strip()
    if ' (gguf-only)' in s:
        s = s.split(' (gguf-only)')[0].strip()
    if ' · ' in s:
        s = s.split(' · ')[0].strip()
    return s


def tag_local(name):
    """[LCL] prefix + usability tags for dropdown display.

    Label-only: `qwen2.5-7b...` -> `[LCL] qwen2.5-7b... · reasoning · slow`.
    _clean strips it back, so load/unload/ping/tune paths are unaffected.
    """
    n = (name or '').strip()
    if ' (gguf-only)' in n:
        base, suffix = n.split(' (gguf-only)', 1)
        try:
            import local_tags as _lt
            return _lt.local_dropdown_label(base.strip()) + ' (gguf-only)'
        except Exception:
            pass
        if n.startswith(('[LCL]', '[CLD]')):
            return n
        return f'[LCL] {n}'
    try:
        import local_tags as _lt
        return _lt.local_dropdown_label(n)
    except Exception:
        pass
    if n.startswith(('[LCL]', '[CLD]')):
        return n
    return f'[LCL] {n}'


# ---------------------------------------------------------------------------
# ollama state
# ---------------------------------------------------------------------------

def ensure_ollama_running():
    if requests is None:
        return False
    try:
        requests.get(f'{OLLAMA_URL}/api/tags', timeout=2)
        return True
    except Exception:
        pass
    try:
        log_file = open('/tmp/ollama.log', 'a')
        env = os.environ.copy()
        env['OLLAMA_HOST'] = '127.0.0.1:11434'
        subprocess.Popen(['ollama', 'serve'], stdout=log_file, stderr=subprocess.STDOUT, env=env)
        time.sleep(2)
        return True
    except Exception:
        return False


def ollama_tags():
    """All imported model names. Never raises."""
    if requests is None:
        return []
    try:
        r = requests.get(f'{OLLAMA_URL}/api/tags', timeout=5).json()
        return [m['name'] for m in r.get('models', [])]
    except Exception:
        return []


def ollama_models():
    """All imported models WITH details (family/parameter_size). Never raises."""
    if requests is None:
        return []
    try:
        r = requests.get(f'{OLLAMA_URL}/api/tags', timeout=5).json()
        return r.get('models', [])
    except Exception:
        return []


def ollama_pull(name, on_line=None):
    """Pull a model via Ollama /api/pull (streaming). Returns (ok, msg).

    `on_line(status)` (optional) receives each human-readable progress status
    from Ollama so a UI can show a live label. Never raises.
    """
    def _emit(s):
        try:
            if on_line:
                on_line(str(s))
        except Exception:
            pass
    if requests is None:
        return False, 'requests missing'
    name = (name or '').strip()
    if not name:
        return False, 'empty model name'
    try:
        import requests as _rq
        last = ''
        with _rq.post(
            f'{OLLAMA_URL}/api/pull',
            json={'name': name, 'stream': True},
            stream=True,
            timeout=(10, 3600),
        ) as resp:
            if resp.status_code != 200:
                return False, f'pull {name}: HTTP {resp.status_code}'
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                if data.get('error'):
                    return False, f"pull {name}: {data['error']}"
                st = data.get('status', '')
                if st:
                    last = st
                    _emit(st)
        return True, f'pulled {name} ({last})'
    except Exception as e:
        return False, f'pull {name} failed: {e}'


def _family_core(s):
    """'gemma2' -> 'gemma', 'phi3' -> 'phi', 'lfm2' -> 'lfm'."""
    return re.sub(r'\d+', '', (s or '').lower()).strip('-_. ')


def gguf_is_imported(gguf_name, models=None):
    """True when a GGUF file's model family is already in Ollama, so it should
    NOT show as '(gguf-only)' in the dropdown.

    The GGUF filename (gemma-2-2b-it-q4_k_m) rarely matches the import name
    (gemma-ultrafast:2b) literally — but both share the same family
    (gemma2 -> gemma). Matching on the family core kills the duplicate entries
    where an already-imported GGUF showed up again as 'gguf-only'."""
    if not gguf_name:
        return False
    name_l = gguf_name.lower()
    for m in (models or ollama_models()):
        det = m.get('details') or {}
        fams = det.get('families') or []
        if not fams:
            fam = det.get('family') or ''
            if fam:
                fams = [fam]
        for fam in fams:
            core = _family_core(fam)
            if core and core in name_l:
                return True
    return False


def ollama_ps():
    """Resident (in-RAM) models. Never raises."""
    if requests is None:
        return []
    try:
        r = requests.get(f'{OLLAMA_URL}/api/ps', timeout=5).json()
        return r.get('models', [])
    except Exception:
        return []


def loaded_names():
    try:
        return [m.get('name', '') for m in ollama_ps()]
    except Exception:
        return []


def load_model(name, timeout=600, base_dir=None):
    """Single-model policy: evict EVERYTHING resident first, then warm the
    target. This box cannot hold two models (phi3 4.1G + qwen 2.2G = OOM
    territory). Returns (ok, msg). BLOCKS (thread it in UI).

    Short-circuits when the target is ALREADY in RAM (e.g. loaded manually via
    `ollama run`) — no point evicting everything + sleeping 2s + re-warming a
    model that's already resident.

    If base_dir is given, applies the model's options from
    Models/model_tuning.json (num_thread/num_ctx) to the warm-up call, so the
    resident model actually runs with the tuned speed settings."""
    clean = _clean(name)
    if requests is None:
        return False, "requests missing"
    # Block up to `timeout` for a competing load — never interleave.
    if not _LOAD_LOCK.acquire(timeout=timeout):
        return False, "Another model is loading — try again when it finishes"
    try:
        resident = ollama_ps()
        base = re.sub(r':.*$', '', clean).strip().lower()

        def _b(n):
            return re.sub(r':.*$', '', (n or '')).strip().lower()

        if any(_b(m.get('name', '')) == base for m in resident):
            return True, f"Already in RAM: {clean} (skipped reload)"

        for m in resident:
            other = m.get('name', '')
            if other and other != clean:
                try:
                    requests.post(f'{OLLAMA_URL}/api/generate',
                                  json={"model": other, "keep_alive": 0}, timeout=15)
                except Exception:
                    pass
        time.sleep(2)  # let Ollama release VRAM/RAM before the next load
        # think=False: thinking models (granite) otherwise burn 30-60s of CPU
        # reasoning about the warmup word "ok" — the load looks minutes slow.
        payload = {"model": clean, "prompt": "ok",
                   "stream": False, "keep_alive": "30m", "think": False}
        if base_dir:
            try:
                opts = get_tuning(base_dir, clean) or {}
                if opts:
                    payload["options"] = opts
            except Exception:
                pass
        r = requests.post(f'{OLLAMA_URL}/api/generate', json=payload,
                          timeout=timeout)
        if r.status_code != 200:
            return False, f"Load failed ({r.status_code}): {r.text[:200]}"
        # Post-load verify: evict anything that isn't the target (a racing
        # `ollama run` or aichat call can land a second model mid-load) and
        # confirm only the target remains.
        time.sleep(1)
        try:
            for m in ollama_ps():
                other = m.get('name', '')
                if other and _b(other) != base:
                    try:
                        requests.post(f'{OLLAMA_URL}/api/generate',
                                      json={"model": other, "keep_alive": 0}, timeout=15)
                    except Exception:
                        pass
        except Exception:
            pass
        return True, f"Loaded: {clean} (others evicted)"
    except Exception as e:
        return False, f"Load error: {e}"
    finally:
        try:
            _LOAD_LOCK.release()
        except Exception:
            pass


def unload_model(name, timeout=15):
    clean = _clean(name)
    if requests is None:
        return False
    try:
        requests.post(f'{OLLAMA_URL}/api/generate',
                      json={"model": clean, "keep_alive": 0}, timeout=timeout)
        return True
    except Exception:
        return False


def unload_all():
    for n in ollama_tags():
        unload_model(n)


def _model_context_length(name):
    """Return a model's context window from Ollama metadata (fallback 8192)."""
    clean = _clean(name)
    base = re.sub(r':.*$', '', clean).strip().lower()
    try:
        for m in ollama_models():
            mb = re.sub(r':.*$', '', m.get('name', '')).strip().lower()
            if mb == base:
                ctx = (m.get('details') or {}).get('context_length')
                if ctx:
                    return int(ctx)
    except Exception:
        pass
    return 8192


def set_aichat_model(name, config_path=None):
    """Point the aichat CLI at `name` (ollama:<name>) in ~/.config/aichat/config.yaml.

    This is what makes the Control Center's "Load & Launch" actually take
    effect: loading a model into RAM is pointless if the chat CLI still reads
    its own model from config.yaml.

    NOTE: this is a TEXT edit, not a yaml.safe_load/safe_dump round-trip. The
    round-trip rewrote `wrap: no` into `wrap: false`, and aichat 0.30 rejects
    the boolean form outright ("Invalid wrap value") — killing every chat turn.
    Returns (ok, msg)."""
    clean = _clean(name)
    path = config_path or os.path.expanduser('~/.config/aichat/config.yaml')
    if not os.path.isfile(path):
        return False, f"aichat config not found: {path}"
    try:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.read().splitlines()
    except Exception as e:
        return False, f"config read failed: {e}"

    # 1. Replace the top-level `model:` line (preserve everything else verbatim).
    out = []
    replaced = False
    for ln in lines:
        if re.match(r'^\s*model\s*:', ln):
            out.append(f'model: ollama:{clean}')
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        out.insert(0, f'model: ollama:{clean}')

    # 2. Ensure the model is listed under the ollama client's `models:` block
    #    (so aichat picks up a sensible max_input_tokens hint). Text-based:
    #    find `name: ollama` then its following `models:` line, and insert a
    #    new `- name:` entry with matching indentation. Uses the model's REAL
    #    context length (not a hardcoded 8192) — phi3 is 4096, and a mismatch
    #    lets aichat send a prompt that Ollama rejects ("Exceed max_input_tokens").
    ctx = max(2048, min(_model_context_length(clean), 8192))
    # Drop stale "(gguf-only)" entries — a name line AND its following
    # max_input_tokens line, as a PAIR. (The old code removed only the name
    # line and left its max_input_tokens orphaned, which broke the YAML block
    # mapping → "did not find expected key" on next aichat run.)
    cleaned = []
    skip_next = False
    for ln in out:
        if re.match(r'^\s*-\s*name\s*:.*\(gguf-only\)\s*$', ln):
            skip_next = True
            continue
        if skip_next and re.match(r'^\s*max_input_tokens\s*:', ln):
            skip_next = False
            continue
        skip_next = False
        cleaned.append(ln)
    out = cleaned
    if not any(re.match(r'^\s*-\s*name\s*:\s*' + re.escape(clean) + r'\s*$', ln)
               for ln in out):
        ollama_idx = None
        for i, ln in enumerate(out):
            if re.match(r'^\s*name\s*:\s*ollama\s*$', ln):
                ollama_idx = i
                break
        if ollama_idx is not None:
            models_idx = None
            for i in range(ollama_idx, len(out)):
                if re.match(r'^\s*models\s*:\s*$', out[i]):
                    models_idx = i
                    break
            if models_idx is not None:
                entry_indent = '      '
                for i in range(models_idx + 1, len(out)):
                    m2 = re.match(r'^(\s*)-\s*name\s*:', out[i])
                    if m2:
                        entry_indent = m2.group(1)
                        break
                out.insert(models_idx + 1,
                           f'{entry_indent}- name: {clean}')
                out.insert(models_idx + 2,
                           f'{entry_indent}  max_input_tokens: {ctx}')

    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(out) + '\n')
    except Exception as e:
        return False, f"config write failed: {e}"
    return True, f"aichat model -> ollama:{clean}"


def get_aichat_active_model(config_path=None):
    """Top-level `model:` line from config.yaml. Never raises ('' if unread)."""
    path = config_path or os.path.expanduser('~/.config/aichat/config.yaml')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for ln in f.read().splitlines():
                m = re.match(r'^\s*model\s*:\s*(.+?)\s*$', ln)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return ''


def set_aichat_cloud_model(provider, model_id, api_key, base_url, config_path=None):
    """Point the aichat CLI at `provider:model_id` (openrouter only for now).

    TEXT edit like set_aichat_model (no yaml round-trip — aichat 0.30 rejects
    the boolean `wrap: false` form). Ensures a client block `name: <provider>`
    with api_base + api_key exists, ensures the model is listed under it,
    then sets top-level `model: <provider>:<model_id>`. Rest untouched.
    Returns (ok, msg)."""
    provider = (provider or 'openrouter').strip() or 'openrouter'
    mid = (model_id or '').strip()
    if not mid:
        return False, 'empty cloud model id'
    mid = re.sub(r'^\[(LCL|CLD)\]\s*', '', mid).strip()
    path = config_path or os.path.expanduser('~/.config/aichat/config.yaml')
    if not os.path.isfile(path):
        return False, f"aichat config not found: {path}"
    try:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.read().splitlines()
    except Exception as e:
        return False, f"config read failed: {e}"

    # 1. Ensure client block `name: <provider>` with api_base + api_key.
    prov_idx = None
    for i, ln in enumerate(lines):
        if re.match(r'^\s*name\s*:\s*' + re.escape(provider) + r'\s*$', ln):
            prov_idx = i
            break
    if prov_idx is None:
        lines.append(f'  - type: openai-compatible')
        lines.append(f'    name: {provider}')
        lines.append(f'    api_base: {base_url}')
        lines.append(f'    api_key: {api_key}')
        lines.append(f'    models:')
        lines.append(f'      - name: {mid}')
        lines.append(f'        max_input_tokens: 8192')
    else:
        # Update api_base / api_key lines within this client block (up to the
        # next `  - type:` line or EOF). Insert missing ones after name:.
        end = len(lines)
        for i in range(prov_idx + 1, len(lines)):
            if re.match(r'^\s*-\s*type\s*:', lines[i]):
                end = i
                break
        has_base = has_key = has_models = False
        for i in range(prov_idx + 1, end):
            if re.match(r'^\s*api_base\s*:', lines[i]):
                lines[i] = f'    api_base: {base_url}'
                has_base = True
            elif re.match(r'^\s*api_key\s*:', lines[i]):
                lines[i] = f'    api_key: {api_key}'
                has_key = True
            elif re.match(r'^\s*models\s*:\s*$', lines[i]):
                has_models = True
        if not has_base:
            lines.insert(prov_idx + 1, f'    api_base: {base_url}')
            end += 1
        if not has_key:
            lines.insert(prov_idx + 2, f'    api_key: {api_key}')
            end += 1
        if not has_models:
            lines.insert(end, f'    models:')
            lines.insert(end + 1, f'      - name: {mid}')
            lines.insert(end + 2, f'        max_input_tokens: 8192')
            end += 3
        else:
            if not any(re.match(r'^\s*-\s*name\s*:\s*' + re.escape(mid) + r'\s*$', ln)
                       for ln in lines[prov_idx:end]):
                for i in range(prov_idx + 1, end):
                    if re.match(r'^\s*models\s*:\s*$', lines[i]):
                        lines.insert(i + 1, f'      - name: {mid}')
                        lines.insert(i + 2, f'        max_input_tokens: 8192')
                        break

    # 2. Top-level `model:` -> `provider:model_id` (strip any old prefix).
    out = []
    replaced = False
    for ln in lines:
        if re.match(r'^\s*model\s*:', ln):
            out.append(f'model: {provider}:{mid}')
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        out.insert(0, f'model: {provider}:{mid}')

    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(out) + '\n')
    except Exception as e:
        return False, f"config write failed: {e}"
    return True, f"aichat model -> {provider}:{mid}"


PING_PROMPT = "explain in summary TBI and PTSD in context of synaptic loss"


def ping_model(name, prompt=None, timeout=180, max_tokens=60):
    """Probe a model's response time with a REAL workload prompt (the TBI/PTSD
    line), so the number reflects actual usefulness, not a bare "ping".

    Returns (ok, report). report = {'model','family','ttft','total','warm',
    'text'} or {'error'}. 'warm' = already in RAM before the ping (pure speed);
    'cold' = had to load first (timing includes the load)."""
    clean = _clean(name)
    if requests is None:
        return False, {'error': 'requests missing'}
    prompt = prompt or PING_PROMPT

    def _base(n):
        return re.sub(r':.*$', '', (n or '')).strip().lower()

    warm = any(_base(m.get('name', '')) == _base(clean) for m in ollama_ps())
    family = clean
    try:
        for m in ollama_models():
            if _base(m.get('name', '')) == _base(clean):
                family = (m.get('details') or {}).get('family') or clean
                break
    except Exception:
        pass

    t0 = time.time()
    first_ts = None
    last_ts = None
    text = ''
    try:
        with requests.post(
            f'{OLLAMA_URL}/api/generate',
            json={"model": clean, "prompt": prompt, "stream": True,
                  "options": {"num_predict": max_tokens}},
            stream=True, timeout=timeout,
        ) as r:
            if r.status_code != 200:
                return False, {'error': f'HTTP {r.status_code}: {r.text[:200]}'}
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except Exception:
                    continue
                if first_ts is None and chunk.get('response'):
                    first_ts = time.time()
                if chunk.get('response'):
                    text += chunk['response']
                if chunk.get('done'):
                    last_ts = time.time()
                    break
                last_ts = time.time()
        total = (last_ts or time.time()) - t0
        ttft = (first_ts - t0) if first_ts else None
        return True, {
            'model': clean,
            'family': family,
            'ttft': ttft,
            'total': total,
            'warm': warm,
            'text': text.strip()[:80],
        }
    except Exception as e:
        return False, {'error': str(e)}


def log_ping(report, base_dir=None):
    """Append a ping result to Models/ping_log.json (last N kept). Never raises."""
    import json as _json
    path = os.path.join(base_dir or '.', 'Models', 'ping_log.json')
    try:
        entries = []
        if os.path.isfile(path):
            try:
                entries = _json.loads(open(path, encoding='utf-8').read())
            except Exception:
                entries = []
        entries.append({
            'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
            'model': report.get('model', ''),
            'family': report.get('family', ''),
            'ttft': round(report.get('ttft') or 0, 2),
            'total': round(report.get('total') or 0, 2),
            'warm': bool(report.get('warm')),
        })
        entries = entries[-50:]
        with open(path, 'w', encoding='utf-8') as f:
            _json.dump(entries, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# gguf scan / import / delete-all-traces
# ---------------------------------------------------------------------------

def scan_gguf_folder(base_dir):
    """{normalized_name: path} for every .gguf under Models/. Never raises."""
    root = _gguf_root(base_dir)
    found = {}
    try:
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d not in ('git', 'llama.cpp', 'workspaces')]
            for f in fns:
                if f.endswith('.gguf'):
                    found[normalize_model_name(f, root)] = os.path.join(dp, f)
    except Exception:
        pass
    try:
        with open(os.path.join(root, 'discovered_models.txt'), 'w') as out:
            for name, path in found.items():
                out.write(f'{name} -> {path}\n')
    except Exception:
        pass
    return found


# Correct chat templates per model family. A GGUF imported WITHOUT one of these
# (or without an embedded tokenizer.chat_template) becomes a completion-only
# model that rambles and never stops — the exact "it's broken" loop hit with
# gemma (unsloth) and lfm2-rag. A family LEFT OUT of this map = "trust the
# GGUF's embedded template" (bartowski/unsloth GGUFs normally carry one).
_FAMILY_TEMPLATES = {
    'gemma': (
        '{{ if .System }}<start_of_turn>user\n{{ .System }}<end_of_turn>\n'
        '{{ end }}<start_of_turn>user\n{{ .Prompt }}<end_of_turn>\n'
        '<start_of_turn>model\n'
    ),
    'lfm': (
        '{{ if .System }}<|startoftext|><|im_start|>system\n{{ .System }}<|im_end|>\n'
        '{{ end }}<|im_start|>user\n{{ .Prompt }}<|im_end|>\n'
        '<|im_start|>assistant\n'
    ),
    'qwen': (
        '{{ if .System }}<|im_start|>system\n{{ .System }}<|im_end|>\n'
        '{{ end }}<|im_start|>user\n{{ .Prompt }}<|im_end|>\n'
        '<|im_start|>assistant\n'
    ),
    'phi': (
        '{{ if .System }}<|system|>\n{{ .System }}<|end|>\n{{ end }}'
        '<|user|>\n{{ .Prompt }}<|end|>\n<|assistant|>\n'
    ),
}


def _detect_family(model_name, gguf_path):
    """Best-guess model family from the import name + GGUF filename."""
    hay = f'{model_name or ""} {os.path.basename(gguf_path or "")}'.lower()
    for fam in ('gemma', 'lfm', 'qwen', 'phi', 'llama', 'mistral'):
        if fam in hay:
            return fam
    return None


def import_single_model(model_name, gguf_path, on_line=None, template=None):
    """`ollama create` from GGUF. BLOCKS (thread it). Returns True/False.

    Writes `FROM ./gguf` + sane caps (num_ctx 8192, num_predict 1024,
    repeat_penalty 1.1, temp 0.4) so an import can NEVER (a) ramble forever or
    (b) overflow the panel's context injection — the two "it's broken" bugs.

    TEMPLATE resolution: explicit `template=` wins; else the model's family
    template (gemma/lfm/qwen/phi); else no override = trust the GGUF's embedded
    template."""
    folder = os.path.dirname(gguf_path)
    modelfile_path = os.path.join(folder, 'Modelfile')

    tpl = template
    if not tpl:
        tpl = _FAMILY_TEMPLATES.get(_detect_family(model_name, gguf_path))

    try:
        with open(modelfile_path, 'w') as mf:
            mf.write(f'FROM ./{os.path.basename(gguf_path)}\n')
            mf.write('PARAMETER num_ctx 8192\n')
            mf.write('PARAMETER num_predict 1024\n')
            mf.write('PARAMETER repeat_penalty 1.1\n')
            mf.write('PARAMETER temperature 0.4\n')
            mf.write('PARAMETER top_p 0.9\n')
            if tpl:
                mf.write(f'TEMPLATE """{tpl}"""\n')
    except Exception as e:
        if on_line:
            on_line(f"Modelfile write failed: {e}")
        return False
    if on_line:
        on_line(f"Importing '{model_name}' from {os.path.basename(gguf_path)} ...")
    try:
        proc = subprocess.Popen(['ollama', 'create', model_name, '--file', modelfile_path],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        for raw in proc.stdout:
            line = raw.strip()
            if line and on_line:
                on_line(line[-300:])
        proc.wait(timeout=3600)
        if proc.returncode == 0:
            if on_line:
                on_line(f"imported '{model_name}'.")
            return True
        if on_line:
            on_line(f"FAILED importing '{model_name}' (exit {proc.returncode})")
        return False
    except Exception as e:
        if on_line:
            on_line(f"import error: {e}")
        return False


def delete_model_all_traces(model_name, base_dir):
    """Unload + ollama rm + GGUF + Modelfile + tuning. Returns (ok, msg)."""
    clean = _clean(model_name)
    base = re.sub(r':.*$', '', clean).strip().lower()
    removed = []
    try:
        unload_model(clean)
        r = subprocess.run(['ollama', 'rm', clean], capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            removed.append(f"Ollama entry '{clean}'")
        root = _gguf_root(base_dir)
        for gguf_name, gguf_path in scan_gguf_folder(base_dir).items():
            if base and (gguf_name == base or gguf_name.startswith(base) or base.startswith(gguf_name)):
                try:
                    os.remove(gguf_path)
                    removed.append(os.path.basename(gguf_path))
                except Exception:
                    pass
                try:
                    mf = os.path.join(os.path.dirname(gguf_path), 'Modelfile')
                    if os.path.isfile(mf):
                        os.remove(mf)
                except Exception:
                    pass
                try:
                    folder = os.path.dirname(gguf_path)
                    if folder != root and not os.listdir(folder):
                        os.rmdir(folder)
                except Exception:
                    pass
        try:
            profs = load_model_tuning(base_dir)
            if base in profs or clean in profs:
                profs.pop(base, None)
                profs.pop(clean, None)
                save_model_tuning(base_dir, profs)
                removed.append("tuning profile")
        except Exception:
            pass
        if removed:
            return True, f"Deleted all traces of '{clean}': {', '.join(removed)}"
        return True, f"'{clean}' removed from Ollama (no GGUF/tuning found)"
    except Exception as e:
        return False, f"Delete error: {e}"


# ---------------------------------------------------------------------------
# tuning (Models/model_tuning.json)
# ---------------------------------------------------------------------------

def load_model_tuning(base_dir):
    try:
        with open(_tuning_file(base_dir), 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def save_model_tuning(base_dir, profiles):
    try:
        with open(_tuning_file(base_dir), 'w') as f:
            json.dump(profiles, f, indent=2)
        return True
    except Exception:
        return False


def get_tuning(base_dir, model_name):
    return load_model_tuning(base_dir).get(_clean(model_name), {}).get('options', {})


def set_tuning(base_dir, model_name, options):
    clean = _clean(model_name)
    profs = load_model_tuning(base_dir)
    prof = profs.get(clean, {})
    prof['mode'] = 'custom'
    prof['options'] = {k: v for k, v in (options or {}).items() if v not in (None, '')}
    profs[clean] = prof
    return save_model_tuning(base_dir, profs)


# ---------------------------------------------------------------------------
# huggingface downloader (requests only, except the final fetch)
# ---------------------------------------------------------------------------

def search_hf_models(org, search="", limit=50):
    """[(repo_id, downloads), ...] — author filter is case-SENSITIVE, so an
    empty exact hit retries fuzzy + case-insensitive client-side."""
    if requests is None:
        return [("requests missing", 0)]

    def _fetch(params):
        resp = requests.get("https://huggingface.co/api/models", params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    try:
        params = {"filter": "gguf", "limit": limit, "sort": "downloads", "direction": -1}
        if org:
            params["author"] = org
        if search:
            params["search"] = search
        results = [(m.get("id", ""), m.get("downloads", 0)) for m in _fetch(params) if m.get("id")]
        if not results and org:
            params2 = {"filter": "gguf", "limit": max(limit, 100), "sort": "downloads", "direction": -1}
            params2["search"] = f"{org} {search}".strip() if search else org
            want = org.lower()
            results = [(m.get("id", ""), m.get("downloads", 0)) for m in _fetch(params2)
                       if m.get("id") and want in m["id"].lower()]
        return results[:limit] or [("No models found.", 0)]
    except Exception as e:
        return [(f"Search failed: {e}", 0)]


def list_gguf_files(repo_id):
    """[(filename, size_bytes), ...] for one repo. Never raises."""
    if requests is None:
        return [("requests missing", 0)]
    try:
        resp = requests.get(f"https://huggingface.co/api/models/{repo_id}/tree/main", timeout=20)
        resp.raise_for_status()
        return [(x.get("path", ""), x.get("size", 0)) for x in resp.json()
                if x.get("path", "").endswith(".gguf")]
    except Exception as e:
        return [(f"File list failed: {e}", 0)]


# ---------------------------------------------------------------------------
# vault helpers (thin wrappers over auth_store; guarded so the module works
# without the vault file present)
# ---------------------------------------------------------------------------

def _vault_mod():
    try:
        import auth_store
        return auth_store
    except Exception:
        return None


def import_from_opencode():
    """Pull deepseek/openrouter keys from opencode's auth.json straight into
    the encrypted vault. Returns (ok, msg)."""
    import pathlib
    v = _vault_mod()
    if v is None:
        return False, "auth_store.py missing"
    try:
        if not v.is_unlocked():
            return False, "Unlock the vault first, then import."
    except Exception as e:
        return False, f"Vault error: {e}"
    oc_auth = pathlib.Path.home() / '.local/share/opencode/auth.json'
    if not oc_auth.exists():
        return False, f"opencode auth not found: {oc_auth}"
    try:
        oc = json.loads(oc_auth.read_text())
    except Exception as e:
        return False, f"Could not read opencode auth: {e}"
    base_urls = {
        'deepseek': 'https://api.deepseek.com/v1',
        'openrouter': 'https://openrouter.ai/api/v1',
        # kimi-for-coding / zhipuai-coding-plan NOT imported (credits issue).
    }
    added, skipped = [], []
    try:
        existing = v.services()
    except Exception:
        existing = []
    for prov in base_urls:
        cfg = oc.get(prov) if isinstance(oc, dict) else None
        key = (cfg or {}).get('key') if isinstance(cfg, dict) else None
        if not key:
            continue
        if prov in existing:
            skipped.append(prov)
            continue
        try:
            v.set_credential(prov, 'api_key', key)
            v.set_credential(prov, 'base_url', base_urls[prov])
            added.append(prov)
        except Exception:
            pass
    if added:
        return True, f"Imported into vault: {', '.join(added)}" + \
            (f" (skipped existing: {', '.join(skipped)})" if skipped else "")
    if skipped:
        return True, f"Already in vault: {', '.join(skipped)}"
    return False, "No provider keys found in opencode auth."


def download_hf_file(repo_id, filename, base_dir, known_size_bytes=0,
                     auto_import=True, delete_after=True, on_progress=None):
    """Download ONE GGUF into Models/{org}/{repo}/. BLOCKS (thread it).
    Returns (ok, msg)."""
    try:
        from huggingface_hub import hf_hub_download
    except Exception:
        return False, "huggingface_hub not installed (pip install huggingface_hub)"
    org = repo_id.split('/')[0] if '/' in repo_id else 'misc'
    repo = repo_id.split('/')[-1]
    target_dir = os.path.join(_gguf_root(base_dir), org, repo)
    os.makedirs(target_dir, exist_ok=True)

    def _prog(path, total_hint=0):
        if on_progress:
            try:
                sz = os.path.getsize(path) if os.path.isfile(path) else 0
                base = known_size_bytes or total_hint or 1
                on_progress(min(99, int(sz * 100 / base)))
            except Exception:
                pass

    try:
        if on_progress:
            on_progress(1)
        local = hf_hub_download(repo_id=repo_id, filename=filename,
                                local_dir=target_dir, local_dir_use_symlinks=False)
        if on_progress:
            on_progress(100)
        msg = f"Downloaded to {local}"
        if auto_import:
            name = normalize_model_name(filename, _gguf_root(base_dir))
            ok = import_single_model(name, local, on_line=on_progress if callable(on_progress) else None)
            msg += f" + imported as {name}" if ok else " (import FAILED)"
            if ok and delete_after:
                try:
                    os.remove(local)
                    msg += " + GGUF deleted"
                except Exception:
                    pass
        return True, msg
    except Exception as e:
        return False, f"Download error: {e}"
