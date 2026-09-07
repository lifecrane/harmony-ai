"""flow_drawio.py — draw.io (mxGraph) adapter for flow_graph.json.

Draw.io stores diagrams as mxGraph XML (`<mxfile><diagram><mxGraphModel>`). This
module converts between our flow_graph JSON (flow_graph.py) and that XML, so the
self-hosted draw.io editor can load/save the same flow the mermaid board shows.

Stdlib-only, no NiceGUI. Imported lazily by the panel when the editor opens.
"""

from __future__ import annotations
import re
import html as _html
import flow_graph

_NODE_W = 200
_NODE_H = 60
_GAP_Y = 40
_MARGIN = 40
_CENTER_X = 300


def _mx_escape(text: str) -> str:
    return _html.escape(text or '', quote=True)


def graph_to_mxgraph_xml(graph: dict) -> str:
    """flow_graph JSON -> draw.io mxGraph XML (simple vertical auto-layout)."""
    nodes = graph.get('nodes', [])
    edges = graph.get('edges', [])
    # map node id -> (x, y) with a simple vertical chain layout
    pos = {}
    for i, n in enumerate(nodes):
        label = n.get('label', '')
        w = max(_NODE_W, min(400, len(label) * 7 + 40))
        pos[n['id']] = (_CENTER_X - w // 2, _MARGIN + i * (_NODE_H + _GAP_Y))

    cells = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>']
    for n in nodes:
        nid = n['id']
        label = _mx_escape(n.get('label', ''))
        x, y = pos[nid]
        w = max(_NODE_W, min(400, len(n.get('label', '')) * 7 + 40))
        shape = n.get('shape', 'rect')
        style = ('rounded=1;arcSize=50;whiteSpace=wrap;html=1;'
                 if shape == 'stadium' else 'rounded=0;whiteSpace=wrap;html=1;')
        cells.append(
            f'<mxCell id="{nid}" value="{label}" style="{style}" vertex="1" parent="1">'
            f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{_NODE_H}" as="geometry"/>'
            f'</mxCell>'
        )
    for i, e in enumerate(edges):
        cells.append(
            f'<mxCell id="e{i}" style="edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;" '
            f'edge="1" parent="1" source="{e["from"]}" target="{e["to"]}">'
            f'<mxGeometry relative="1" as="geometry"/>'
            f'</mxCell>'
        )
    return (
        '<mxfile host="aichat" version="1.0">'
        '<diagram id="flow" name="Flow">'
        '<mxGraphModel dx="800" dy="600" grid="1" gridSize="10" guides="1" tooltips="1" '
        'connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="850" pageHeight="1100">'
        '<root>' + ''.join(cells) + '</root>'
        '</mxGraphModel>'
        '</diagram>'
        '</mxfile>'
    )


def mxgraph_xml_to_graph(xml: str) -> dict:
    """draw.io mxGraph XML -> flow_graph JSON (edges + vertex cells only)."""
    nodes, edges = [], []
    for m in re.finditer(r'<mxCell[^>]*>', xml or ''):
        tag = m.group(0)
        if 'edge="1"' in tag:
            src = re.search(r'source="([^"]+)"', tag)
            tgt = re.search(r'target="([^"]+)"', tag)
            if src and tgt and src.group(1) not in ('0', '1') and tgt.group(1) not in ('0', '1'):
                edges.append({'from': src.group(1), 'to': tgt.group(1)})
        elif 'vertex="1"' in tag:
            nid = re.search(r'id="([^"]+)"', tag)
            val = re.search(r'value="([^"]*)"', tag)
            if nid and nid.group(1) not in ('0', '1'):
                label = _html.unescape(val.group(1)) if val else ''
                style = tag
                shape = 'stadium' if 'rounded=1' in style else 'rect'
                nodes.append({
                    'id': nid.group(1),
                    'label': label,
                    'shape': shape,
                    'done': flow_graph._detect_done(label),
                })
    return {'version': flow_graph.VERSION, 'title': '', 'nodes': nodes,
            'edges': edges, 'comments': {}}
