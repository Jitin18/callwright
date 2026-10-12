import { useEffect, useState } from "react";

const CALLER_ID_HELP = "Calling number (ANI). For FreshCart Online, a number matching the order skips the phone check.";
import type { CallState } from "../lib/types";

export function TopBar({ state, view, setView, onStart, onEnd, replaying }: {
  state: CallState; view: "stage" | "engineer"; setView: (v: "stage" | "engineer") => void;
  onStart: (callerNumber?: string) => void; onEnd: () => void; replaying?: string | null;
}) {
  const [caller, setCaller] = useState("");
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 500); return () => clearInterval(t); }, []);
  const secs = state.active && state.startedAt ? Math.floor((now - state.startedAt) / 1000) : 0;
  const status = replaying ? "Replay" : state.active ? "On call" : state.ended ? "Call ended" : "Ready";
  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-mark" aria-hidden><i /><i /><i /></span>
        <div>
          <h1>Callwright</h1>
          <p>FreshCart demo · Rewards · Online · live agent floor</p>
        </div>
      </div>
      <div className="topbar-mid">
        <span className={`status status--${state.active ? "live" : "idle"}`}><i />{status}</span>
        {state.active && <span className="timer">{String(Math.floor(secs / 60)).padStart(2, "0")}:{String(secs % 60).padStart(2, "0")}</span>}
        {state.active && !state.llmKey && <span className="warn">The voice cascade is not configured on the runtime (GROQ_API_KEY)</span>}
        {replaying && <span className="warn">Replaying recorded call: {replaying}</span>}
      </div>
      <div className="topbar-right">
        <div className="seg" role="tablist">
          <button role="tab" aria-selected={view === "stage"} className={view === "stage" ? "is-on" : ""} onClick={() => setView("stage")}>Stage</button>
          <button role="tab" aria-selected={view === "engineer"} className={view === "engineer" ? "is-on" : ""} onClick={() => setView("engineer")}>Engineer</button>
        </div>
        {state.active
          ? <button className="btn btn--end" onClick={onEnd}>End call</button>
          : <>
              <label className="caller" title={CALLER_ID_HELP}>
                <span>Caller ID</span>
                <input value={caller} onChange={(e) => setCaller(e.target.value)} placeholder="withheld" inputMode="tel" />
              </label>
              <button className="btn btn--start" onClick={() => onStart(caller)}>Start call</button>
            </>}
      </div>
    </header>
  );
}
