"""viking.py — Viking context plugin (OpenViking integration).

The three functions for the agent chain:
  vput(path, data)    -> store data in the Viking context DB (embedded + searchable)
  vctx(path, tokens)  -> retrieve compressed context for a stored path (token-budgeted)
  vfind(query, tokens)-> semantic search, best matches within a token budget

Backed by OpenViking (openviking / openviking-sdk): a local context-database
server on 127.0.0.1:1933 with embeddings from the LOCAL Ollama model
(qwen3-embedding:0.6b) — no cloud involved.

House rules honored:
  * LAZY — the SDK is imported only on first call, so boot cost stays zero.
  * NEVER RAISES — every function returns a string (the text to inject into
    context, or a "[viking] ..." status line) so the chat/pipeline chain
    degrades gracefully when the server is down.
  * THREAD-SAFE — the pipeline runs model calls in background threads.

Store layout: user paths are kept under the `resources` scope, so
  vput("projects/pacman/plan.md", ...)
lands at
  viking://resources/projects/pacman/plan.md
"""

from __future__ import annotations

import os
import re
import threading

# Server URL (override with OPENVIKING_URL if it ever moves).
SERVER_URL = os.environ.get('OPENVIKING_URL', 'http://127.0.0.1:1933')

_client = None
_client_lock = threading.Lock()

# Token budget defaults when the caller doesn't give one.
DEFAULT_CTX_TOKENS = 300
DEFAULT_FIND_TOKENS = 500

# How many candidates vfind pulls before fitting them into the budget.
FIND_CANDIDATES = 6


def _get_client():
    """Lazy singleton SDK client. Returns None if the SDK is missing."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                try:
                    from openviking_sdk import SyncHTTPClient
                    c = SyncHTTPClient(url=SERVER_URL)
                    c.initialize()
                    _client = c
                except Exception:
                    _client = False  # SDK not installed — don't retry-loop hard
    return _client or None


def offline_msg(err: str = '') -> str:
    extra = f" ({err})" if err else ''
    return (f"[viking] context server offline at {SERVER_URL}{extra} — "
            f"start it with: openviking-server")


def is_online() -> bool:
    c = _get_client()
    if c is None:
        return False
    try:
        return bool(c.is_healthy())
    except Exception:
        return False


def status() -> str:
    """One-line health report (for chat / diagnostics)."""
    c = _get_client()
    if c is None:
        return offline_msg('openviking-sdk not importable in this python')
    try:
        if not c.is_healthy():
            return offline_msg()
        return f"[viking] online at {SERVER_URL}"
    except Exception as e:
        return offline_msg(str(e)[:120])


def _norm_path(path: str) -> str:
    """User path -> viking://resources/... URI."""
    p = (path or '').strip()
    p = re.sub(r'^viking://', '', p)
    p = re.sub(r'^/', '', p)
    return f'viking://resources/{p}'


def _token_len(text: str) -> int:
    # Whitespace split is a good-enough token estimate for budgets.
    return len((text or '').split())


def _clip_to_tokens(text: str, tokens: int) -> str:
    if tokens <= 0:
        tokens = 1
    words = (text or '').split()
    if len(words) <= tokens:
        return text
    return ' '.join(words[:tokens]) + f'  …[clipped to {tokens} tokens]'


def _read_content(c, uri: str) -> str:
    """Read node content, tolerating the folder/file layout
    (a file stored as <dir>/<name>.md is readable at <dir>/<name>.md/<name>.md,
    or under a renamed staging file inside that folder)."""
    try:
        return c.read(uri) or ''
    except Exception:
        pass
    base = uri.rstrip('/').rsplit('/', 1)[-1]
    try:
        return c.read(f'{uri}/{base}') or ''
    except Exception:
        pass
    # Directory node with a single file child (staging renamed it)?
    try:
        entries = c.ls(uri) or []
        files = [e for e in entries if not e.get('isDir')]
        if len(files) == 1:
            return c.read(files[0].get('uri') or f"{uri}/{files[0].get('name')}") or ''
    except Exception:
        pass
    return ''


# ---------------------------------------------------------------------------
# vput(path, data) — store data
# ---------------------------------------------------------------------------

def vput(path: str, data) -> str:
    """Store `data` (str or dict) under `path` in the Viking context DB.

    New paths are ingested (parsed + embedded); existing paths are replaced.
    Returns a short confirmation or a "[viking] ..." error line. Never raises.
    """
    c = _get_client()
    if c is None:
        return offline_msg()
    if not is_online():
        return offline_msg()
    uri = _norm_path(path)
    try:
        if isinstance(data, dict):
            import json as _json
            content = _json.dumps(data, indent=2, ensure_ascii=False)
        else:
            content = str(data if data is not None else '')
        if not content.strip():
            return f"[viking] vput: nothing to store at '{path}'"

        # Does the target already exist as a file? Create vs replace
        # (write only supports existing files; create fails if it exists).
        target_file_uri = uri
        exists_as_file = False
        try:
            st = c.stat(uri) or {}
            # A directory node (leftover from an old add_resource ingest)
            # is not stored file content; the real text lives one level down.
            if st.get('isDir'):
                base = uri.rstrip('/').rsplit('/', 1)[-1] or 'content.md'
                target_file_uri = f'{uri.rstrip("/")}/{base}'
            else:
                exists_as_file = True
        except Exception:
            pass
        try:
            st2 = c.stat(target_file_uri) or {}
            if not st2.get('isDir'):
                exists_as_file = True
        except Exception:
            pass

        mode = 'replace' if exists_as_file else 'create'
        try:
            c.write(target_file_uri, content, mode=mode, wait=True, timeout=180)
        except Exception as write_exc:
            # A stale directory sits on the target file path (older vput used
            # add_resource and made <path>/<name> a directory). Recover by
            # writing to the canonical <path>/<name> file inside it.
            if mode == 'create':
                base = uri.rstrip('/').rsplit('/', 1)[-1] or 'content.md'
                parent = uri.rstrip('/')
                inner = f'{parent}/{base}'
                c.write(inner, content, mode='create', wait=True, timeout=180)
                target_file_uri = inner
            else:
                raise write_exc

        verb = 'replaced' if mode == 'replace' else 'stored'
        return f"[viking] {verb} '{path}' ({len(content)} chars, embedded)"
    except Exception as e:
        return f"[viking] vput failed for '{path}': {str(e)[:160]}"


# ---------------------------------------------------------------------------
# vctx(path, tokens) — retrieve compressed context
# ---------------------------------------------------------------------------

def vctx(path: str, tokens: int = DEFAULT_CTX_TOKENS) -> str:
    """Retrieve the (compressed) context stored at `path`, within `tokens`.

    Prefers OpenViking's own compressed abstract when a VLM is configured;
    falls back to the raw content clipped to the token budget (embedding-only
    setups have no abstract, so clipping is the compression). Never raises.
    """
    c = _get_client()
    if c is None:
        return offline_msg()
    if not is_online():
        return offline_msg()
    try:
        tokens = int(tokens)
    except (TypeError, ValueError):
        tokens = DEFAULT_CTX_TOKENS
    uri = _norm_path(path)

    # Compressed form first (needs a VLM; embedding-only says "not ready").
    try:
        abstract = c.abstract(uri) or ''
        if abstract and 'not ready' not in abstract.lower():
            clipped = _clip_to_tokens(abstract, tokens)
            return f"[viking: {path} — abstract]\n{clipped}"
    except Exception:
        pass

    content = _read_content(c, uri)
    if not content:
        return f"[viking] nothing stored at '{path}' yet (vput it first)"
    return f"[viking: {path}]\n{_clip_to_tokens(content, tokens)}"


# ---------------------------------------------------------------------------
# vfind(query, tokens) — semantic search
# ---------------------------------------------------------------------------

def vfind(query: str, tokens: int = DEFAULT_FIND_TOKENS) -> str:
    """Semantic search across everything stored in Viking.

    Returns the best-matching content packed within `tokens`, each hit headed
    by its URI + score. Never raises.
    """
    c = _get_client()
    if c is None:
        return offline_msg()
    if not is_online():
        return offline_msg()
    try:
        tokens = int(tokens)
    except (TypeError, ValueError):
        tokens = DEFAULT_FIND_TOKENS
    try:
        res = c.search(query=query, limit=FIND_CANDIDATES) or {}
    except Exception as e:
        return f"[viking] vfind failed: {str(e)[:160]}"

    hits = []
    for key in ('resources', 'memories', 'skills'):
        for item in res.get(key) or []:
            uri = item.get('uri') or ''
            if not uri or uri.endswith('.overview.md'):
                continue
            hits.append((float(item.get('score') or 0), uri, item.get('abstract') or ''))
    hits.sort(key=lambda h: -h[0])
    if not hits:
        return f"[viking] no matches for '{query}' (store something with vput first)"

    parts = []
    used = 0
    for score, uri, abstract in hits:
        content = abstract or _read_content(c, uri)
        if not content:
            continue
        header = f"--- {uri} (score {score:.2f}) ---"
        room = max(20, tokens - used - _token_len(header) - 2)
        body = _clip_to_tokens(content, room)
        parts.append(f"{header}\n{body}")
        used += _token_len(header) + _token_len(body) + 2
        if used >= tokens:
            break
    return f"[viking: search '{query}']\n" + '\n\n'.join(parts)


# ---------------------------------------------------------------------------
# vls() — bonus: what's stored (used by the tag parser for diagnostics)
# ---------------------------------------------------------------------------

def vls() -> str:
    c = _get_client()
    if c is None or not is_online():
        return offline_msg()
    try:
        entries = c.ls('viking://resources') or []
        if not entries:
            return '[viking] store is empty'
        names = [e.get('uri', '') for e in entries]
        return '[viking] stored: ' + ', '.join(names[:20])
    except Exception as e:
        return f"[viking] ls failed: {str(e)[:160]}"