import { useEffect, useMemo, useRef, useState } from "react";
import { layoutGraph } from "../lib/layout";
import type { JourneyGraph, RunView } from "../lib/types";

const NW = 150, NH = 44;

/** The SOP flowchart, live: visited path lit, current step glowing, decisions show the branch taken. */
export function SopFlow({ runs, order, focus, onFocus, catalogue }: {
  runs: Record<string, RunView>; order: string[]; focus?: string; onFocus: (id: string) => void; catalogue: JourneyGraph[];
}) {
  const [preview, setPreview] = useState<string>("idv.online_order");
  const run = focus ? runs[focus] : undefined;
  const graph = run?.graph ?? catalogue.find((g) => g.id === preview);

  return (
    <section className="sop panel" aria-label="Live SOP">
      <header className="panel-head">
        <h2>Live SOP</h2>
        <nav className="sop-tabs">
          {order.length === 0 && catalogue.length > 0 && (
            <select value={preview} onChange={(e) => setPreview(e.target.value)} aria-label="Preview a journey">
              {catalogue.map((g) => <option key={g.id} value={g.id}>{g.title}</option>)}
            </select>
          )}
          {order.map((id) => {
            const r = runs[id];
            return (
              <button key={id} className={`tab tab--${r.status}${id === focus ? " is-on" : ""}`} onClick={() => onFocus(id)}>
                <i />{r.title}
              </button>
            );
          })}
        </nav>
      </header>
      {graph ? <Graph graph={graph} run={run} /> : <p className="empty">Journeys load from the agent runtime.</p>}
    </section>
  );
}

function Graph({ graph, run }: { graph: JourneyGraph; run?: RunView }) {
  const { placed, width, height } = useMemo(() => layoutGraph(graph, 184, 60), [graph]);
  const scroller = useRef<HTMLDivElement>(null);
  const visited = new Set(run?.visited ?? []);
  const current = run?.node;
  const endStatus = run && ["completed", "failed", "handoff", "redirected"].includes(run.status) ? run.status : null;

  useEffect(() => {
    if (!current || !scroller.current) return;
    const p = placed.get(current);
    if (p) scroller.current.scrollTo({ left: Math.max(0, p.x - scroller.current.clientWidth / 2 + NW / 2), top: Math.max(0, p.y - 60), behavior: "smooth" });
  }, [current, placed]);

  const taken = (from: string, to: string, label: string | null) => {
    if (!visited.has(from) || !visited.has(to)) return false;
    if (label !== null && run?.branches[from] !== undefined) return run.branches[from] === label;
    const seq = run?.visited ?? [];
    return seq.some((n, i) => n === from && seq[i + 1] === to);
  };

  return (
    <div className="sop-scroll" ref={scroller}>
      <div className="sop-canvas" style={{ width, height: height + 30 }}>
        <svg width={width} height={height + 30} className="sop-edges">
          <defs>
            <marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0 0 8 4 0 8z" className="arrowhead" />
            </marker>
          </defs>
          {graph.edges.map((e, i) => {
            const a = placed.get(e.from), b = placed.get(e.to);
            if (!a || !b) return null;
            const back = b.x <= a.x;
            const d = back
              ? `M${a.x + NW / 2},${a.y + NH} C${a.x + NW / 2},${a.y + NH + 46} ${b.x + NW / 2},${b.y + NH + 46} ${b.x + NW / 2},${b.y + NH}`
              : `M${a.x + NW},${a.y + NH / 2} C${a.x + NW + 30},${a.y + NH / 2} ${b.x - 30},${b.y + NH / 2} ${b.x},${b.y + NH / 2}`;
            const on = taken(e.from, e.to, e.label);
            return (
              <g key={i} className={`edge${on ? " is-taken" : ""}${back ? " is-back" : ""}`}>
                <path d={d} markerEnd="url(#arrow)" />
                {e.label && (
                  <text x={back ? (a.x + b.x) / 2 + NW / 2 : (a.x + NW + b.x) / 2} y={back ? Math.max(a.y, b.y) + NH + 40 : (a.y + b.y) / 2 + NH / 2 - 6}>
                    {e.label}
                  </text>
                )}
              </g>
            );
          })}
        </svg>
        {graph.nodes.map((n) => {
          const p = placed.get(n.id)!;
          const cls = [
            "node", `node--${n.kind}`, `lane--${n.lane}`,
            visited.has(n.id) && "is-visited",
            n.id === current && (run?.status === "awaiting_input" ? "is-waiting" : endStatus ? `is-end is-${endStatus}` : "is-current"),
          ].filter(Boolean).join(" ");
          return (
            <div key={n.id} className={cls} style={{ left: p.x, top: p.y, width: NW, height: NH }} title={n.label}>
              <span className="node-kind" aria-hidden />
              <span className="node-label">{n.label}</span>
              {run?.branches[n.id] && <span className="node-branch">{run.branches[n.id]}</span>}
            </div>
          );
        })}
      </div>
    </div>
  );
}
