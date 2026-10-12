import { useEffect, useState } from "react";
import { Captions } from "./components/Captions";
import { EngineerView } from "./components/EngineerView";
import { Floor } from "./components/Floor";
import { Guardrails } from "./components/Guardrails";
import { HandoverCard } from "./components/HandoverCard";
import { NowHappening } from "./components/NowHappening";
import { SopFlow } from "./components/SopFlow";
import { Systems } from "./components/Systems";
import { TopBar } from "./components/TopBar";
import { TurnDeck } from "./components/TurnDeck";
import { VoiceOrb } from "./components/VoiceOrb";
import { useCall } from "./lib/useCall";

export default function App() {
  const call = useCall();
  const { state } = call;
  const [view, setView] = useState<"stage" | "engineer">("stage");
  const [text, setText] = useState("");
  const [hidden, setHidden] = useState<string | null>(null);

  // ?replay=<scenario>[&speed=2][&stop=40000]: play a recorded call through the UI (demos, screenshots; no Azure).
  useEffect(() => {
    const q = new URLSearchParams(location.search);
    const name = q.get("replay");
    if (name) call.replay(name, Number(q.get("speed") || 1), q.get("stop") ? Number(q.get("stop")) : undefined);
    if (q.get("view") === "engineer") setView("engineer");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const busy = Object.entries(state.agents).some(([k, a]) => k !== "coordinator" && a.state !== "idle");
  const mode = !state.active ? "idle"
    : call.levels.current.playing || state.agents.coordinator.state === "speaking" ? "speaking"
    : busy ? "thinking" : "listening";
  const card = state.summary ?? state.handover;
  const cardKey = card ? `${card.kind}-${card.reason ?? card.disposition}` : null;

  return (
    <div className={`app sentiment--${state.sentiment}`}>
      <TopBar state={state} view={view} setView={setView} onStart={(c) => call.startCall(c)} onEnd={call.endCall}
        replaying={call.replaying} />
      <main className="grid">
        <aside className="voice panel">
          <VoiceOrb levels={call.levels} mode={mode} />
          <div className="voice-meta">
            <span className={`mode mode--${mode}`}>{{ idle: "Not on a call", listening: "Listening", thinking: "Agents working", speaking: "Speaking" }[mode]}</span>
            {state.active && <span className="mood">Mood: <b>{state.sentiment}</b></span>}
          </div>
          <Captions captions={state.captions} />
          <form className="say" onSubmit={(e) => { e.preventDefault(); call.sendText(text); setText(""); }}>
            <button type="button" className={`mic${call.micOn ? " is-on" : ""}`} disabled={!state.active}
              onClick={() => (call.micOn ? call.stopMic() : call.startMic())} aria-label={call.micOn ? "Mute microphone" : "Unmute microphone"}>
              <svg viewBox="0 0 24 24" aria-hidden><path d="M12 15a3 3 0 0 0 3-3V6a3 3 0 1 0-6 0v6a3 3 0 0 0 3 3Zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.9V21h2v-2.1A7 7 0 0 0 19 12h-2Z" /></svg>
            </button>
            <input value={text} onChange={(e) => setText(e.target.value)} disabled={!state.active}
              placeholder={state.active ? "Or type what the customer says…" : "Start a call to talk to the assistant"} aria-label="Customer message" />
            <button className="send" disabled={!state.active || !text.trim()}>Send</button>
          </form>
          {call.micError && <p className="notice">{call.micError}</p>}
          {state.errors.length > 0 && <p className="notice notice--error">{state.errors[state.errors.length - 1].where}: {state.errors[state.errors.length - 1].message}</p>}
          <Guardrails guards={state.guards} />
        </aside>
        <section className="floor-panel panel">
          <NowHappening steps={state.steps} />
          <Floor state={state} onPacketDone={call.packetDone} />
          {card && hidden !== cardKey && <HandoverCard h={card} onClose={() => setHidden(cardKey)} />}
        </section>
        <Systems systems={state.systems} />
        {view === "stage" ? (
          <>
            <SopFlow runs={state.runs} order={state.runOrder} focus={state.focusRun} onFocus={call.focus} catalogue={call.catalogue} />
            <TurnDeck turns={state.turns} />
          </>
        ) : (
          <div className="engineer-slot"><EngineerView state={state} /></div>
        )}
      </main>
    </div>
  );
}
