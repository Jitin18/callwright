import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { MicStreamer, PcmPlayer } from "./audio";
import { AGENT_NAME, SYSTEMS, SUBAGENT_FOR_JOURNEY, actionOf, systemOf } from "./floor";
import type { AgentKey, CallState, JourneyGraph, Packet, ScopeKey, Step, SystemKey } from "./types";

const AGENT_KEYS: AgentKey[] = ["orchestrator", "idv", "rewards", "online", "enrolment", "knowledge", "escalation", "coordinator"];
const SYSTEM_KEYS = Object.keys(SYSTEMS) as SystemKey[];
// Handoffs are queued and shown one at a time so a presenter can follow them.
export const HANDOFF_DWELL_MS = 1200;
const MAX_QUEUE = 6;
const FUNCTION_WORDS: Record<string, string> = {
  route_request: "working out what's needed", submit_answer: "passing on the customer's answer",
  transfer_to_colleague: "arranging a colleague", end_call: "wrapping up the call",
};

const initial = (): CallState => ({
  connected: false, llmKey: true, active: false, ended: false,
  agents: Object.fromEntries(AGENT_KEYS.map((k) => [k, { state: "idle" }])) as CallState["agents"],
  involved: [], queue: [], packets: [], tools: [], steps: [],
  systems: Object.fromEntries(SYSTEM_KEYS.map((k) => [k, { status: "idle", calls: 0 }])) as CallState["systems"],
  runs: {}, runOrder: [], captions: [], guards: {}, turns: [], sentiment: "neutral", errors: [], events: [],
});

type Action =
  | { type: "event"; e: any }
  | { type: "packetDone"; id: string }
  | { type: "release" }
  | { type: "toolExpire"; id: string }
  | { type: "focus"; runId: string }
  | { type: "reset" }
  | { type: "local"; patch: Partial<CallState> };

let seq = 0;
const pid = () => `p${++seq}`;

function reduce(s: CallState, a: Action): CallState {
  switch (a.type) {
    case "reset": return initial();
    case "local": return { ...s, ...a.patch };
    case "focus": return { ...s, focusRun: a.runId };
    case "packetDone": return { ...s, packets: s.packets.filter((p) => p.id !== a.id) };
    case "release": return s.queue.length ? { ...s, packets: [s.queue[0]], queue: s.queue.slice(1) } : s;
    case "toolExpire": return { ...s, tools: s.tools.filter((t) => t.id !== a.id) };
    case "event": break;
  }
  const e = a.e;
  const next: CallState = { ...s, events: e.type === "audio.delta" ? s.events : [...s.events.slice(-599), e] };
  const enqueue = (p: Omit<Packet, "id">) => { next.queue = [...next.queue, { ...p, id: pid() }].slice(-MAX_QUEUE); };
  const involve = (k: string) => {
    if ((AGENT_KEYS as string[]).includes(k) && !next.involved.includes(k as AgentKey)) next.involved = [...next.involved, k as AgentKey];
  };
  const setAgent = (k: AgentKey, patch: Partial<CallState["agents"][AgentKey]>) => {
    next.agents = { ...next.agents, [k]: { ...next.agents[k], ...patch } };
  };
  const step = (st: Omit<Step, "at">) => {
    const i = next.steps.findIndex((x) => x.id === st.id);
    const full = { ...st, at: e.t ?? 0 };
    next.steps = i >= 0 ? next.steps.map((x, j) => (j === i ? { ...x, ...full } : x)) : [...next.steps.slice(-29), full];
  };

  switch (e.type) {
    case "session.ready":
      next.connected = true; next.sessionId = e.session_id; next.llmKey = e.voice_configured; break;
    case "turn.started":
      next.captions = [...s.captions, { id: e.turn_id, role: "customer", text: e.text }];
      next.turns = [...s.turns, { id: e.turn_id, text: e.text, intents: [] }];
      involve("coordinator");
      step({ id: `turn-${e.turn_id}`, path: ["Customer", "Coordinator"], text: e.text, status: "info" });
      break;
    case "agent.state":
      if ((AGENT_KEYS as string[]).includes(e.agent)) {
        setAgent(e.agent, { state: e.state, label: e.label });
        if (e.state !== "idle") involve(e.agent);
      }
      if (e.agent === "coordinator" && e.state === "thinking" && FUNCTION_WORDS[e.label])
        step({ id: `fn-${e.t}`, path: ["Coordinator"], text: FUNCTION_WORDS[e.label], status: "info" });
      break;
    case "coordinator.plan":
      next.lastPlan = { intents: e.intents };
      next.turns = s.turns.map((t, i) => (i === s.turns.length - 1 ? { ...t, intents: e.intents, sentiment: e.sentiment } : t));
      break;
    case "sentiment": next.sentiment = e.value; break;
    case "handoff": {
      const subagent = (e.target as AgentKey) ?? SUBAGENT_FOR_JOURNEY(e.journey);
      involve(subagent); involve("coordinator"); involve("orchestrator");
      if (!e.resume) {
        enqueue({ kind: "handoff", from: e.source === "coordinator" ? "orchestrator" : e.source, to: subagent, label: e.title });
        step({ id: `run-${e.run_id}`, path: ["Coordinator", "Orchestrator", `${AGENT_NAME(subagent)} subagent`], text: e.title, status: "active" });
      }
      const prev = s.runs[e.run_id];
      next.runs = { ...s.runs, [e.run_id]: prev ?? {
        runId: e.run_id, journey: e.journey, title: e.title, subagent, graph: e.graph as JourneyGraph | undefined,
        status: "running", visited: [], branches: {}, startedAt: e.t } };
      if (!prev) next.runOrder = [...s.runOrder, e.run_id];
      next.focusRun = e.run_id;
      break;
    }
    case "handoff.return":
      if (["completed", "failed", "handoff", "redirected"].includes(e.status))
        enqueue({ kind: "return", from: e.source, to: "orchestrator" });
      break;
    case "journey.node": {
      const r = s.runs[e.run_id];
      if (!r) break;
      const upd = { ...r, node: e.node };
      if (e.phase === "enter") upd.visited = [...r.visited, e.node];
      if (typeof e.phase === "string" && e.phase.startsWith("branch:")) upd.branches = { ...r.branches, [e.node]: e.phase.slice(7) };
      if (e.phase === "waiting") upd.status = "awaiting_input";
      next.runs = { ...s.runs, [e.run_id]: upd };
      next.focusRun = e.run_id;
      break;
    }
    case "journey.status": {
      const r = s.runs[e.run_id];
      if (r) {
        next.runs = { ...s.runs, [e.run_id]: { ...r, status: e.status, outcome: e.outcome } };
        const path = ["Orchestrator", `${AGENT_NAME(r.subagent)} subagent`];
        if (["completed", "failed", "handoff", "redirected", "error"].includes(e.status))
          step({ id: `run-${e.run_id}`, path, text: r.title, status: e.status === "failed" || e.status === "error" ? "error" : "ok",
                 detail: String(e.outcome || e.status).replace(/_/g, " ") });
        else if (e.status === "awaiting_input")
          step({ id: `run-${e.run_id}`, path, text: r.title, status: "active", detail: "waiting for the customer" });
      }
      break;
    }
    case "tool.call": {
      const system = systemOf(e.tool);
      next.tools = [...s.tools, { id: e.id, scope: e.scope as ScopeKey, tool: e.tool, label: e.label, done: false, system }];
      const sys = s.systems[system];
      next.systems = { ...s.systems, [system]: { ...sys, status: "working", action: actionOf(e.tool), calls: sys.calls + 1, at: e.t } };
      if (e.tool !== "log_contact_outcome")
        step({ id: e.id, path: ["Coordinator", `${AGENT_NAME(e.agent)} subagent`, SYSTEMS[system].name], text: actionOf(e.tool), status: "active" });
      break;
    }
    case "tool.result": {
      const t = s.tools.find((x) => x.id === e.id);
      next.tools = s.tools.map((x) => (x.id === e.id ? { ...x, done: true, ms: e.ms, ok: e.ok, summary: e.summary } : x));
      if (t) {
        const sys = s.systems[t.system];
        next.systems = { ...s.systems, [t.system]: { ...sys, status: e.ok ? "ok" : "error", last: e.summary, ms: e.ms, at: e.t } };
        if (t.tool !== "log_contact_outcome")
          step({ id: e.id, path: ["Coordinator", `${AGENT_NAME(e.agent)} subagent`, SYSTEMS[t.system].name], text: actionOf(t.tool),
                 status: e.ok ? "ok" : "error", detail: e.summary });
      }
      break;
    }
    case "guardrail":
      next.guards = { ...s.guards, [e.name]: { stage: e.stage, name: e.name, status: e.status, detail: e.detail, ms: e.ms, at: e.t } };
      if (e.status === "block") {
        step({ id: `g-${e.t}-${e.name}`, path: ["Guardrails"], text: `${e.stage} check stopped: ${e.name.replace(/_/g, " ")}`,
               status: "error", detail: e.detail ?? undefined });
        if (e.stage === "input") next.turns = s.turns.map((t, i) => (i === s.turns.length - 1 ? { ...t, blocked: true } : t));
      }
      break;
    case "assistant.say":
      next.captions = [...s.captions, { id: `${e.turn_id}-${s.captions.length}`, role: "assistant", text: e.text, kind: e.kind }];
      break;
    case "turn.done":
      next.turns = s.turns.map((t) => (t.id === e.turn_id ? { ...t, latency: e.latency } : t));
      break;
    case "handover.card":
      next.handover = { ...e, kind: "handover" };
      step({ id: `handover-${e.run_id}`, path: ["Escalation", "ContactHub", "Colleague"], text: "Warm transfer with handover note",
             status: "ok", detail: e.reason });
      break;
    case "call.summary":
      next.summary = { ...e, kind: "summary" };
      step({ id: "outcome", path: ["Orchestrator", "CRM"], text: "Call outcome logged", status: "ok",
             detail: String(e.disposition).replace(/_/g, " ") });
      break;
    case "call.ended":
      next.active = false; next.ended = true; break;
    case "error":
      next.errors = [...s.errors.slice(-9), { where: e.where, message: e.message, at: e.t }]; break;
  }
  return next;
}

export interface Levels { mic: number; out: number; inSpeech: boolean; playing: boolean }

export function useCall() {
  const [state, dispatch] = useReducer(reduce, undefined, initial);
  const [catalogue, setCatalogue] = useState<JourneyGraph[]>([]);
  const [micOn, setMicOn] = useState(false);
  const [micError, setMicError] = useState<string | null>(null);
  const [replaying, setReplaying] = useState<string | null>(null);
  const ws = useRef<WebSocket | null>(null);
  const rec = useRef<MicStreamer | null>(null);
  const player = useRef<PcmPlayer | null>(null);
  const levels = useRef<Levels>({ mic: 0, out: 0, inSpeech: false, playing: false });
  const lastRelease = useRef(0);

  useEffect(() => {
    fetch("/api/journeys").then((r) => r.json()).then((d) => setCatalogue(d.journeys)).catch(() => {});
  }, []);

  // Release queued handoff animations one at a time, each shown for at least HANDOFF_DWELL_MS.
  useEffect(() => {
    if (state.packets.length || !state.queue.length) return;
    const wait = Math.max(0, lastRelease.current + HANDOFF_DWELL_MS - Date.now());
    const t = setTimeout(() => { lastRelease.current = Date.now(); dispatch({ type: "release" }); }, wait);
    return () => clearTimeout(t);
  }, [state.packets.length, state.queue.length]);

  const getPlayer = () => {
    if (!player.current) {
      player.current = new PcmPlayer((playing) => { levels.current.playing = playing; });
    }
    return player.current;
  };

  const onMessage = useCallback((msg: MessageEvent) => {
    const e = JSON.parse(msg.data);
    if (e.type === "audio.delta") { getPlayer().play(e.audio); return; }   // streamed voice
    if (e.type === "audio.clear") getPlayer().clear();                        // barge-in
    dispatch({ type: "event", e });
    if (e.type === "tool.result") setTimeout(() => dispatch({ type: "toolExpire", id: e.id }), 2600);
  }, []);

  const startMic = useCallback(async () => {
    if (rec.current?.active) return;
    const r = new MicStreamer(
      (pcm) => { if (ws.current?.readyState === WebSocket.OPEN) ws.current.send(pcm); },
      (rms) => { levels.current.mic = rms; levels.current.inSpeech = rms > 0.02; },
      () => player.current?.audible() ?? false,
    );
    try {
      await r.start();
      rec.current = r;
      setMicOn(true);
      setMicError(null);
    } catch (err) {
      setMicError("Microphone unavailable. Allow mic access, or type below.");
    }
  }, []);

  const stopMic = useCallback(() => { rec.current?.stop(); rec.current = null; setMicOn(false); }, []);

  const startCall = useCallback(async (callerNumber?: string) => {
    dispatch({ type: "reset" });
    getPlayer().unlock();
    await getPlayer().route();   // before the greeting arrives; inside the click so the browser allows playback
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const sock = new WebSocket(`${proto}://${location.host}/ws`);
    ws.current = sock;
    sock.onmessage = onMessage;
    sock.onclose = () => dispatch({ type: "local", patch: { connected: false, active: false } });
    await new Promise<void>((resolve, reject) => { sock.onopen = () => resolve(); sock.onerror = () => reject(); })
      .catch(() => dispatch({ type: "event", e: { type: "error", where: "connection", message: "Could not reach the agent runtime on /ws.", t: 0 } }));
    if (sock.readyState !== WebSocket.OPEN) return;
    dispatch({ type: "local", patch: { active: true, startedAt: Date.now() } });
    sock.send(JSON.stringify({ type: "start", caller_number: callerNumber || null }));
    startMic();
  }, [onMessage, startMic]);

  const endCall = useCallback(() => {
    ws.current?.send(JSON.stringify({ type: "end" }));
    stopMic();
    setTimeout(() => ws.current?.close(), 8000);
  }, [stopMic]);

  const sendText = useCallback((text: string) => {
    if (ws.current?.readyState === WebSocket.OPEN && text.trim()) ws.current.send(JSON.stringify({ type: "text", text }));
  }, []);

  /** Play a recorded call (recordings/<name>.jsonl) through the UI with its original timing: no Azure needed. */
  const replay = useCallback(async (name: string, speed = 1, stopAt?: number) => {
    const r = await fetch(`/api/recordings/${encodeURIComponent(name)}`);
    if (!r.ok) { dispatch({ type: "event", e: { type: "error", where: "replay", message: `No recording '${name}'`, t: 0 } }); return; }
    const events: any[] = (await r.text()).split("\n").filter(Boolean).map((l) => JSON.parse(l));
    dispatch({ type: "reset" });
    dispatch({ type: "local", patch: { active: true, startedAt: Date.now() } });
    setReplaying(name);
    const t0 = events[0]?.t ?? 0;
    const start = performance.now();
    for (const e of events) {
      if (stopAt != null && e.t - t0 > stopAt) break;
      const wait = (e.t - t0) / speed - (performance.now() - start);
      if (wait > 0) await new Promise((res) => setTimeout(res, wait));
      dispatch({ type: "event", e });
      if (e.type === "tool.result") setTimeout(() => dispatch({ type: "toolExpire", id: e.id }), 2600);
    }
    setReplaying(null);
  }, []);

  useEffect(() => {
    let raf = 0;
    const tick = () => { levels.current.out = player.current?.level() ?? 0; raf = requestAnimationFrame(tick); };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);

  return {
    state, catalogue, levels, micOn, micError, startCall, endCall, sendText, startMic, stopMic, replay, replaying,
    packetDone: (id: string) => dispatch({ type: "packetDone", id }),
    focus: (runId: string) => dispatch({ type: "focus", runId }),
  };
}
