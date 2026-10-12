import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { AGENTS, point } from "../lib/floor";
import { HANDOFF_DWELL_MS } from "../lib/useCall";
import type { AgentKey, AgentState, CallState, Packet } from "../lib/types";

const DEFAULT_VIEW = { rx: 47, rz: -38 };
const HEIGHT: Record<AgentState, number> = { idle: 16, thinking: 44, delegating: 44, working: 60, listening: 40, speaking: 54 };
const STATE_WORDS: Record<AgentState, string> = {
  idle: "Idle", thinking: "Thinking", delegating: "Delegating", working: "Working", listening: "Listening", speaking: "Speaking",
};

/** The agent floor: only the agents taking part in this call are lit; one handoff is shown at a time. */
export function Floor({ state, onPacketDone }: { state: CallState; onPacketDone: (id: string) => void }) {
  const wrap = useRef<HTMLDivElement>(null);
  const [W, setW] = useState(520);
  const [view, setView] = useState(DEFAULT_VIEW);
  const drag = useRef<{ x: number; y: number; rx: number; rz: number } | null>(null);

  useLayoutEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      setW(Math.max(240, Math.min(width / 1.5, (height - 20) / 1.02)));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const heightOf = (k: AgentKey) => HEIGHT[state.agents[k].state] + (k === "orchestrator" ? 10 : 0);
  const involved = new Set(state.involved);
  const quiet = !state.active && !state.ended;

  return (
    <div
      ref={wrap}
      className="floor-stage"
      style={{ ["--rx" as string]: `${view.rx}deg`, ["--rz" as string]: `${view.rz}deg`, ["--W" as string]: `${W}px` }}
      onPointerDown={(e) => { drag.current = { x: e.clientX, y: e.clientY, ...view }; (e.target as Element).setPointerCapture?.(e.pointerId); }}
      onPointerMove={(e) => {
        const d = drag.current;
        if (!d) return;
        setView({ rz: d.rz + (e.clientX - d.x) * 0.35, rx: Math.max(30, Math.min(70, d.rx - (e.clientY - d.y) * 0.25)) });
      }}
      onPointerUp={() => (drag.current = null)}
      onDoubleClick={() => setView(DEFAULT_VIEW)}
      aria-label="Agent floor. Drag to orbit, double-click to reset."
    >
      <div className="floor">
        <div className="floor-surface" />
        <Wires state={state} W={W} involved={involved} />
        {(Object.keys(AGENTS) as AgentKey[]).map((k) => {
          const a = state.agents[k];
          const busy = a.state !== "idle";
          const dim = !quiet && !involved.has(k);
          return (
            <Block key={k} x={AGENTS[k].x * W} y={AGENTS[k].y * W} size={AGENTS[k].size} h={heightOf(k)}
              variant={k === "coordinator" ? "voice" : k === "orchestrator" ? "lead" : "subagent"}
              active={busy} dim={dim} title={AGENTS[k].name}
              subtitle={busy ? (a.label || STATE_WORDS[a.state]) : quiet ? AGENTS[k].role : undefined} />
          );
        })}
        {state.packets.map((p) => (
          <PacketFx key={p.id} p={p} W={W} view={view} heights={(k) => heightOf(k as AgentKey)} onDone={onPacketDone} />
        ))}
      </div>
      <p className="floor-hint">Drag to orbit · double-click to reset</p>
    </div>
  );
}

function Block(props: {
  x: number; y: number; size: number; h: number; variant: "lead" | "subagent" | "voice";
  active: boolean; dim: boolean; title: string; subtitle?: string;
}) {
  const { x, y, size, h, variant, active, dim, title, subtitle } = props;
  return (
    <div className={`block block--${variant}${active ? " is-active" : ""}${dim ? " is-dim" : ""}`}
      style={{ left: x, top: y, ["--s" as string]: `${size}px`, ["--h" as string]: `${h}px` }}>
      <div className="face face-n" /><div className="face face-e" />
      <div className="face face-s" /><div className="face face-w" />
      <div className="face face-top" />
      <div className="shadow" />
      <div className="billboard">
        <strong>{title}</strong>
        {subtitle && <span>{subtitle}</span>}
      </div>
    </div>
  );
}

function Wires({ state, W, involved }: { state: CallState; W: number; involved: Set<AgentKey> }) {
  const c = AGENTS.orchestrator;
  return (
    <svg className="wires" width={W} height={W} viewBox={`0 0 ${W} ${W}`}>
      {(Object.keys(AGENTS) as AgentKey[]).filter((k) => k !== "orchestrator").map((k) => {
        const hot = state.agents[k].state !== "idle";
        const on = involved.has(k);
        return (
          <line key={k} x1={c.x * W} y1={c.y * W} x2={AGENTS[k].x * W} y2={AGENTS[k].y * W}
            className={`wire${k === "coordinator" ? " wire--voice" : ""}${on ? " is-on" : ""}${hot ? " is-hot" : ""}`} />
        );
      })}
    </svg>
  );
}

function PacketFx({ p, W, view, heights, onDone }: {
  p: Packet; W: number; view: { rx: number; rz: number }; heights: (k: string) => number; onDone: (id: string) => void;
}) {
  const el = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const a = point(p.from), b = point(p.to);
    if (!a || !b || !el.current) { onDone(p.id); return; }
    const za = heights(p.from) + 6, zb = heights(p.to) + 6;
    const bill = `rotateZ(${-view.rz}deg) rotateX(${-view.rx}deg)`;
    const at = (x: number, y: number, z: number) => `translate3d(${x * W}px, ${y * W}px, ${z}px) ${bill}`;
    const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const anim = el.current.animate(
      [
        { transform: at(a.x, a.y, za), opacity: 0 },
        { transform: at(mid.x, mid.y, Math.max(za, zb) + 46), opacity: 1, offset: 0.5 },
        { transform: at(b.x, b.y, zb), opacity: 1, offset: 0.85 },
        { transform: at(b.x, b.y, zb), opacity: 0 },
      ],
      { duration: reduce ? 1 : HANDOFF_DWELL_MS, easing: "cubic-bezier(.4,0,.2,1)", fill: "forwards" },
    );
    anim.onfinish = () => onDone(p.id);
    return () => anim.cancel();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return (
    <div ref={el} className={`packet packet--${p.kind}`}>
      <span className="packet-core" />
      {p.label && p.kind === "handoff" && <span className="packet-label">{p.label}</span>}
    </div>
  );
}
