"""flow_graph.py — flow board as structured JSON (nodes/edges/comments).

Why this exists
---------------
The flow board was authored as `flow.md` (Mermaid flowchart text). That's great
for the LLM to WRITE and for mermaid to RENDER, but it's a dead end for an
interactive editor (draw.io / tldraw): you can't drag/annotate/comment on raw
Mermaid text. So this module owns a small, stable JSON document as the single
source of truth, plus lossless converters to/from `flow.md`.

Editor-agnostic by design — no NiceGUI, no mermaid, no draw.io import here. The
panel and any editor adapter (JSON <-> mxGraph XML for draw.io) both call into
this module.

Schema (flow_graph.json)
------------------------
{
  "version": 1,
  "title": "WeBull",
  "nodes": [
    {"id": "START", "label": "🚀 START", "shape": "stadium"},
    {"id": "T1", "label": "✅ Review supertrend_bot.py ...", "shape": "rect", "done": true}
  ],
  "edges": [
    {"from": "START", "to": "T1"}
  ],
  "comments": { "T1": [{"author": "joao", "text": "...", "ts": "2026-09-06T00:00:00"}] }
}

`done` is derived from the leading ✅/⬜ marker when present. `shape` is
"stadium" (rounded, for start/end) or "rect". Comments are keyed by node id and
survive re-generation of flow.md (they never live in the Mermaid text).
"""

from __future__ import annotations
import json
import re
import os
from pathlib import Path
from typing import Optional

VERSION = 1

_NODE_STADIUM_RE = re.compile(r'^(\w[\w\-]*)\s*\(\[(.*?)\]\)\s*$')
_NODE_RECT_RE = re.compile(r'^(\w[\w\-]*)\s*\["(.*?)"\]\s*$')
_NODE_PLAIN_RE = re.compile(r'^(\w[\w\-]*)\s*\[(.*?)\]\s*$')
_EDGE_RE = re.compile(r'^(\w[\w\-]*)\s*-{2,}>[|]?(.*?)[|]?\s*(\w[\w\-]*)\s*$')
_DONE_MARK = re.compile(r'^\s*(✅|✔|\[x\]|\[X\])\s*')


def _detect_done(label: str) -> bool:
    return bool(_DONE_MARK.match(label or ''))


def parse_flow_md(text: str) -> dict:
    """Parse Mermaid flowchart text into the JSON graph. Tolerant; never raises."""
    nodes = []
    edges = []
    seen_ids = set()
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line or line.startswith('%%'):
            continue
        if line.lower().startswith(('flowchart', 'graph', 'sequenceDiagram', 'mindmap')):
            continue
        em = _EDGE_RE.match(line)
        if em:
            edges.append({'from': em.group(1), 'to': em.group(3)})
            continue
        for rx, shape in ((_NODE_STADIUM_RE, 'stadium'),
                          (_NODE_RECT_RE, 'rect'),
                          (_NODE_PLAIN_RE, 'rect')):
            m = rx.match(line)
            if m:
                nid, label = m.group(1), m.group(2).strip()
                if nid not in seen_ids:
                    nodes.append({
                        'id': nid,
                        'label': label,
                        'shape': shape,
                        'done': _detect_done(label),
                    })
                    seen_ids.add(nid)
                break
    return {'version': VERSION, 'title': '', 'nodes': nodes, 'edges': edges, 'comments': {}}


def flow_to_md(graph: dict) -> str:
    """Serialize the JSON graph back to Mermaid flowchart text (stable, readable)."""
    lines = ['flowchart TD', '']
    for n in graph.get('nodes', []):
        label = n.get('label', '')
        shape = n.get('shape', 'rect')
        if shape == 'stadium':
            lines.append(f"{n['id']}([{label}])")
        else:
            lines.append(f"{n['id']}[\"{label}\"]")
    lines.append('')
    for e in graph.get('edges', []):
        lines.append(f"{e['from']} --> {e['to']}")
    return '\n'.join(lines) + '\n'


def _flow_paths(project_dir) -> tuple[Path, Path]:
    return Path(project_dir) / 'flow.json', Path(project_dir) / 'flow.md'


def read_flow_graph(project_dir) -> dict:
    """Read the JSON graph; falls back to parsing flow.md if JSON is absent."""
    jp, mp = _flow_paths(project_dir)
    if jp.is_file():
        try:
            return json.loads(jp.read_text(encoding='utf-8'))
        except Exception:
            pass
    if mp.is_file():
        try:
            g = parse_flow_md(mp.read_text(encoding='utf-8'))
            g['title'] = Path(project_dir).name
            return g
        except Exception:
            pass
    return {'version': VERSION, 'title': Path(project_dir).name, 'nodes': [], 'edges': [], 'comments': {}}


def write_flow_graph(project_dir, graph: dict) -> tuple[bool, str]:
    """Persist JSON + regenerate flow.md (mermaid quick-view stays in sync)."""
    try:
        jp, mp = _flow_paths(project_dir)
        graph.setdefault('version', VERSION)
        graph.setdefault('comments', {})
        jp.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding='utf-8')
        mp.write_text(flow_to_md(graph), encoding='utf-8')
        return True, f"saved {jp.name} + {mp.name}"
    except Exception as e:
        return False, f"save failed: {e}"


def add_comment(project_dir, node_id: str, author: str, text: str) -> bool:
    import datetime as _dt
    g = read_flow_graph(project_dir)
    g.setdefault('comments', {}).setdefault(node_id, []).append({
        'author': author, 'text': text, 'ts': _dt.datetime.now().isoformat(timespec='seconds'),
    })
    ok, msg = write_flow_graph(project_dir, g)
    return ok
