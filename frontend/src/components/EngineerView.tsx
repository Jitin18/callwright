import { useEffect, useMemo, useState } from "react";
import type { CallState } from "../lib/types";

const GROUPS: Record<string, string[]> = {
  All: [],
  Agents: ["agent.state", "coordinator.plan", "handoff", "handoff.return", "subagent.extracted", "coordinator.dropped_intent"],
  Tools: ["tool.call", "tool.result"],
  Journeys: ["journey.node", "journey.status"],
  Voice: ["voice.ready", "vad.mode", "vad.updated", "turn.merged", "post_ack_pause", "silence", "audio.clear", "sentiment", "call.summary", "call.ended"],
  Safety: ["guardrail"],
  Speech: ["turn.started", "assistant.say", "turn.done"],
  Errors: ["error"],
};

/** Raw (already redacted) event stream, the system of record, and demo controls. */
export function EngineerView({ state }: { state: CallState }) {
  const [group, setGroup] = useState("All");
  const [sor, setSor] = useState<any>(null);
  const [config, setConfig] = useState<any>(null);

  useEffect(() => { fetch("/api/config").then((r) => r.json()).then(setConfig).catch(() => {}); }, []);
  useEffect(() => {
    const load = () => fetch("/api/admin/state").then((r) => r.json()).then(setSor).catch(() => {});
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, []);

  const events = useMemo(() => {
    const g = GROUPS[group];
    return state.events.filter((e) => !g.length || g.includes(e.type)).slice(-250).reverse();
  }, [state.events, group]);

  const toggleFraud = async () => {
    const r = await fetch("/api/admin/fraud-line", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ available: !sor?.fraud_line_available }) });
    const d = await r.json();
    setSor((s: any) => ({ ...s, ...d }));
  };

  return (
    <div className="engineer">
      <section className="panel ev">
        <header className="panel-head">
          <h2>Event stream</h2>
          <nav className="seg seg--small">
            {Object.keys(GROUPS).map((g) => <button key={g} className={g === group ? "is-on" : ""} onClick={() => setGroup(g)}>{g}</button>)}
          </nav>
        </header>
        <ol className="ev-list">
          {events.map((e, i) => {
            const { type, t, audio, graph, ...rest } = e;
            return (
              <li key={`${t}-${i}`} className={`ev-item ev--${type.split(".")[0]}`}>
                <span className="ev-t">{(t / 1000).toFixed(2)}s</span>
                <span className="ev-type">{type}</span>
                <code>{JSON.stringify(audio ? { ...rest, audio: `‹${Math.round(audio.length * 0.75 / 1024)} KB wav›` } : graph ? { ...rest, graph: "‹graph›" } : rest)}</code>
              </li>
            );
          })}
        </ol>
      </section>
      <section className="panel sor">
        <header className="panel-head"><h2>System of record</h2></header>
        <div className="sor-body">
          <div className="control">
            <span>Fraud line</span>
            <button className={`switch${sor?.fraud_line_available ? " is-on" : ""}`} onClick={toggleFraud} aria-pressed={!!sor?.fraud_line_available}>
              <i />{sor?.fraud_line_available ? "Answering" : "Unavailable"}
            </button>
          </div>
          <SorList title="CRM contact log" rows={sor?.contacts} cols={["contact_id", "journey", "disposition", "outcome", "transfer_reason"]} />
          <SorList title="TaskDesk tasks" rows={sor?.taskdesk_tasks} cols={["task_id", "task_type", "assigned_team"]} />
          <SorList title="Fraud cases" rows={sor?.fraud_cases} cols={["case_id", "txn_id", "points_disputed"]} />
          <SorList title="Transfers (with handover note)" rows={sor?.transfers} cols={["transfer_id", "queue", "connected", "summary"]} />
          {config && (
            <div className="models">
              <h3>Azure Voice Live</h3>
              {["transcription", "model", "tts", "voice", "vad"].map((k) => <p key={k}><span>{k}</span>{String(config[k])}</p>)}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

function SorList({ title, rows, cols }: { title: string; rows?: any[]; cols: string[] }) {
  return (
    <div className="sor-list">
      <h3>{title} <em>{rows?.length ?? 0}</em></h3>
      {(rows ?? []).slice(-5).reverse().map((r, i) => (
        <p key={i}>{cols.map((c) => <span key={c}>{String(r[c] ?? "—")}</span>)}</p>
      ))}
    </div>
  );
}
