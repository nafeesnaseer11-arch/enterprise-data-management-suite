"""Lineage graph queries and Graphviz rendering."""
from __future__ import annotations

import pandas as pd

COLORS = {"aws_s3": "#FF9900", "aws_rds": "#FF9900", "landing": "#4285F4", "bronze": "#CD7F32",
          "silver": "#A8A9AD", "gold": "#D4AF37"}


def _walk(edges: pd.DataFrame, start: str, direction: str) -> set[int]:
    a, b = ("to_node", "from_node") if direction == "up" else ("from_node", "to_node")
    seen_nodes, idx, frontier = {start}, set(), [start]
    while frontier:
        n = frontier.pop()
        for i, r in edges[edges[a] == n].iterrows():
            idx.add(i)
            if r[b] not in seen_nodes:
                seen_nodes.add(r[b])
                frontier.append(r[b])
    return idx


def subgraph(edges: pd.DataFrame, node: str | None) -> pd.DataFrame:
    if edges.empty or not node:
        return edges
    return edges.loc[sorted(_walk(edges, node, "up") | _walk(edges, node, "down"))]


def to_dot(edges: pd.DataFrame, focus: str | None = None) -> str:
    layer_of = {}
    for r in edges.itertuples():
        layer_of[r.from_node], layer_of[r.to_node] = r.from_layer, r.to_layer
    lines = ['digraph L {', 'rankdir=LR; bgcolor="transparent";',
             'node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=10, fontcolor="#111111"];',
             'edge [color="#888888", fontsize=8, fontname="Helvetica", fontcolor="#888888"];']
    for n, layer in layer_of.items():
        pen = ', penwidth=3, color="#E91E63"' if n == focus else ""
        lines.append(f'"{n}" [fillcolor="{COLORS.get(layer, "#DDDDDD")}"{pen}];')
    for r in edges.itertuples():
        lines.append(f'"{r.from_node}" -> "{r.to_node}";')
    lines.append("}")
    return "\n".join(lines)
