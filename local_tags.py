# === SECTION: LOCAL MODEL USABILITY TAGS ===
"""Local-model usability tags — label-only badges for the model dropdown.

Pattern matches niceai.py cloud badges (FEATURES.md: `· free` + `⭐`):
  VALUE stays clean (`[LCL] <ollama-name>`, backend strips it).
  LABEL gets suffix (`[LCL] <name> · fast · chat`).

Persisted in Models/local_tags.json: { "<ollama-name>": ["fast", ...] }.
Fixed vocabulary so a later niceai.py merge just imports this file and
feeds tags into MODEL_CAPABILITIES notes / _cloud_dropdown_label logic.

Rule: absent = not fit / untested. No negative tags — if a model lacks
`reasoning`, the UI treats it as not-for-reasoning. A niche star (e.g.
excellent CAD but bad at chat) is just `["cad"]` with no chat tag.
`drop` = discard pile, sorted last. `keeper` / `default` float to top.
"""
import json
import os

# === SECTION: constants ===
# Groups: speed | role/niche | verdict. Display + chips follow this order.
ALLOWED_TAGS = ["fast", "slow",
                "chat", "coder", "reasoning", "vision", "cad", "docs", "embed",
                "default", "keeper", "drop"]

_TAG_ORDER = {t: i for i, t in enumerate(ALLOWED_TAGS)}

MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Models")
LOCAL_TAGS_FILE = os.path.join(MODELS_DIR, "local_tags.json")

# Seed for the current fleet (i5-6500T, 4 threads, CPU-only).
# fast = >10 tok/s measured, slow = dense + >45s load, default = daily driver,
# keeper = quality keep (slow but worth it, e.g. qwen2.5-7b Wardenclyffe test).
SEED_TAGS = {
    "lfm2-1.2b-rag:latest": ["fast", "chat", "default"],
    "lfm-rag:1.2b": ["fast", "chat"],
    "Gemma-unsloth-q5-k-m:latest": ["fast", "chat"],
    "qwen2-5-coder-3b-instruct-q6-k:latest": ["coder"],
    "qwen3-5-4b-super-coder-q4-0:latest": ["coder"],
    "qwen3.5-4b-q3_k_m:latest": ["reasoning", "chat"],
    "granite-4-2-3b-q5-k-m:latest": ["reasoning"],
    "olmoe-1b-7b-0924-instruct-q4-k-m:latest": ["reasoning", "chat"],
    "qwen2.5-7b-q4_k_m:latest": ["reasoning", "chat", "slow", "keeper"],
    "phi3:reasoning": ["reasoning", "slow"],
}


# === SECTION: io ===
def _ensure_file():
    try:
        if not os.path.exists(LOCAL_TAGS_FILE):
            os.makedirs(MODELS_DIR, exist_ok=True)
            with open(LOCAL_TAGS_FILE, "w", encoding="utf-8") as f:
                json.dump(SEED_TAGS, f, indent=2, sort_keys=True)
    except Exception:
        pass


def read_local_tags():
    """{model-name: [tags]}. Seeds file on first run. Never raises."""
    _ensure_file()
    try:
        with open(LOCAL_TAGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {k: [t for t in (v or []) if t in ALLOWED_TAGS]
                    for k, v in data.items() if isinstance(k, str)}
    except Exception:
        pass
    return dict(SEED_TAGS)


def write_local_tags(mapping):
    """Persist full mapping. Never raises. Returns True on success."""
    try:
        os.makedirs(MODELS_DIR, exist_ok=True)
        clean = {k: [t for t in (v or []) if t in ALLOWED_TAGS]
                 for k, v in (mapping or {}).items() if isinstance(k, str)}
        with open(LOCAL_TAGS_FILE, "w", encoding="utf-8") as f:
            json.dump(clean, f, indent=2, sort_keys=True)
        return True
    except Exception:
        return False


# === SECTION: labels ===
def get_tags(model_name):
    """Tags for one model (clean name, no [LCL] prefix). Never raises."""
    try:
        base = strip_label(model_name)
        return read_local_tags().get(base, [])
    except Exception:
        return []


def set_tags(model_name, tags):
    """Replace tags for one model. Returns True on success."""
    try:
        base = strip_label(model_name)
        mapping = read_local_tags()
        ordered = sorted({t for t in (tags or []) if t in ALLOWED_TAGS},
                         key=lambda t: _TAG_ORDER.get(t, 99))
        mapping[base] = ordered
        return write_local_tags(mapping)
    except Exception:
        return False


def toggle_tag(model_name, tag):
    """Toggle one tag for one model. Returns updated tag list."""
    if tag not in ALLOWED_TAGS:
        return get_tags(model_name)
    cur = get_tags(model_name)
    nxt = [t for t in cur if t != tag] if tag in cur else cur + [tag]
    set_tags(model_name, nxt)
    return nxt


def strip_label(label):
    """`[LCL] name · fast · chat` / `[LCL] name (gguf-only)` -> `name`. Never raises."""
    try:
        s = (label or "").strip()
        for prefix in ("[LCL] ", "[CLD] "):
            if s.startswith(prefix):
                s = s[len(prefix):].strip()
                break
        if " (gguf-only)" in s:
            s = s.split(" (gguf-only)")[0].strip()
        if " · " in s:
            s = s.split(" · ")[0].strip()
        return s
    except Exception:
        return (label or "").strip()


def local_dropdown_label(name):
    """`name` -> `[LCL] name · tag1 · tag2`. Value-safe, label-only badges."""
    try:
        base = strip_label(name)
        tags = read_local_tags().get(base, [])
        tags = sorted(tags, key=lambda t: _TAG_ORDER.get(t, 99))
        prefix = "" if base.startswith(("[LCL]", "[CLD]")) else "[LCL] "
        if not tags:
            return f"{prefix}{base}"
        return f"{prefix}{base} · {' · '.join(tags)}"
    except Exception:
        n = (name or "").strip()
        return n if n.startswith(("[LCL]", "[CLD]")) else f"[LCL] {n}"


# === SECTION: sorting ===
def sort_key_for_label(label):
    """Dropdown order: default/keeper first, drop last, rest alphabetical.

    Returns (group, name). Merge note: niceai.py can reuse this to float
    preferred teams the same way it floats ⭐ clouds today.
    """
    try:
        base = strip_label(label)
        tags = read_local_tags().get(base, [])
        if "drop" in tags:
            grp = 3
        elif "default" in tags or "keeper" in tags:
            grp = 0
        elif tags:
            grp = 1
        else:
            grp = 2
        return (grp, base.lower())
    except Exception:
        return (2, (label or "").lower())
