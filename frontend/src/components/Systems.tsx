import { SYSTEMS } from "../lib/floor";
import type { SystemKey, SystemView } from "../lib/types";

const CHIP: Record<SystemView["status"], string> = { idle: "Idle", working: "Working", ok: "OK", error: "Problem" };

/** The business systems behind the MCP tools, in plain words. Tool names live in the Engineer view. */
export function Systems({ systems }: { systems: Record<SystemKey, SystemView> }) {
  return (
    <section className="systems panel" aria-label="Systems">
      <header className="panel-head"><h2>Systems</h2><span className="panel-sub">reached only through each subagent's own tools</span></header>
      <div className="sys-grid">
        {(Object.keys(SYSTEMS) as SystemKey[]).map((k) => {
          const v = systems[k];
          return (
            <article key={k} className={`sys sys--${v.status}`}>
              <header>
                <h3>{SYSTEMS[k].name}</h3>
                <span className={`sys-chip sys-chip--${v.status}`}>{CHIP[v.status]}</span>
              </header>
              <p className="sys-what">{SYSTEMS[k].what}</p>
              <p className="sys-last">
                {v.status === "working" ? `${cap(v.action)}…` : v.last ? v.last : "Not used yet"}
              </p>
              {v.calls > 0 && <p className="sys-meta">{v.calls} call{v.calls > 1 ? "s" : ""}{v.ms != null ? ` · last ${v.ms} ms` : ""}</p>}
            </article>
          );
        })}
      </div>
    </section>
  );
}

const cap = (s?: string) => (s ? s[0].toUpperCase() + s.slice(1) : "");
