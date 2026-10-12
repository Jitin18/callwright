import type { JourneyGraph } from "./types";

export interface Placed { id: string; x: number; y: number; depth: number }

/** Layered left→right layout: BFS depth from the start node; back-edges (retries) drawn as arcs. */
export function layoutGraph(g: JourneyGraph, colW = 176, rowH = 62) {
  const out = new Map<string, string[]>();
  g.edges.forEach((e) => out.set(e.from, [...(out.get(e.from) ?? []), e.to]));
  const depth = new Map<string, number>([[g.start, 0]]);
  const order: string[] = [g.start];
  for (let i = 0; i < order.length; i++) {
    const n = order[i];
    for (const t of out.get(n) ?? []) {
      if (!depth.has(t)) {
        depth.set(t, depth.get(n)! + 1);
        order.push(t);
      }
    }
  }
  g.nodes.forEach((n) => { if (!depth.has(n.id)) { depth.set(n.id, 0); order.push(n.id); } });
  const rows = new Map<number, number>();
  const placed = new Map<string, Placed>();
  for (const id of order) {
    const d = depth.get(id)!;
    const r = rows.get(d) ?? 0;
    rows.set(d, r + 1);
    placed.set(id, { id, depth: d, x: 24 + d * colW, y: 24 + r * rowH });
  }
  const maxDepth = Math.max(...[...depth.values()]);
  const maxRows = Math.max(...[...rows.values()]);
  return { placed, width: 24 * 2 + (maxDepth + 1) * colW, height: 24 * 2 + maxRows * rowH };
}
