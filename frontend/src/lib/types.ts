export type AgentKey = "orchestrator" | "idv" | "rewards" | "online" | "enrolment" | "knowledge" | "escalation" | "coordinator";
export type AgentState = "idle" | "thinking" | "delegating" | "working" | "listening" | "speaking";
export type ScopeKey =
  | "member_identity" | "order_verification" | "loyalty_accounts" | "order_service"
  | "member_enrolment" | "case_management" | "knowledge_base";
export type SystemKey = "crm" | "online" | "taskdesk" | "contacthub" | "sms" | "rag";

export interface GraphNode { id: string; label: string; lane: string; kind: string; terminal: boolean; status?: string | null }
export interface GraphEdge { from: string; to: string; label: string | null }
export interface JourneyGraph {
  id: string; title: string; section: string; subagent: string; requires_idv: string | null;
  start: string; nodes: GraphNode[]; edges: GraphEdge[];
}

export interface RunView {
  runId: string; journey: string; title: string; subagent: string; graph?: JourneyGraph;
  status: string; node?: string; visited: string[]; branches: Record<string, string>; outcome?: string | null;
  startedAt: number;
}

export interface Packet { id: string; kind: "handoff" | "return"; from: string; to: string; label?: string }
export interface ToolFlash { id: string; scope: ScopeKey; tool: string; label: string; ms?: number; ok?: boolean; summary?: string; done: boolean; system: SystemKey }
export interface SystemView { status: "idle" | "working" | "ok" | "error"; last?: string; action?: string; calls: number; ms?: number; at?: number }
export interface Step { id: string; path: string[]; text: string; status: "active" | "ok" | "error" | "info"; detail?: string; at: number }
export interface Caption { id: string; role: "customer" | "assistant"; text: string; kind?: string; direction?: string | null }
export interface GuardState { stage: string; name: string; status: string; detail?: string | null; ms: number; at: number }
export interface Latency { first_audio?: number | null; tools?: number; post_ack_delay_ms?: number }
export interface TurnView { id: string; text: string; intents: string[]; sentiment?: string; latency?: Latency; blocked?: boolean }
export interface Handover {
  kind: "handover" | "summary"; headline?: string; reason?: string | null; summary?: string; verified?: string;
  customer_mood?: string; transcript?: string[]; disposition?: string; transfer_reason?: string | null; ended_by?: string;
  journey?: string; queue?: string;
}

export interface CallState {
  connected: boolean; sessionId?: string; llmKey: boolean; active: boolean; ended: boolean; startedAt?: number;
  agents: Record<AgentKey, { state: AgentState; label?: string | null; llm?: string | null }>;
  involved: AgentKey[];
  queue: Packet[]; packets: Packet[]; tools: ToolFlash[]; systems: Record<SystemKey, SystemView>; steps: Step[];
  runs: Record<string, RunView>; runOrder: string[]; focusRun?: string;
  captions: Caption[]; guards: Record<string, GuardState>; turns: TurnView[]; sentiment: string;
  handover?: Handover; summary?: Handover; errors: { where: string; message: string; at: number }[]; events: any[];
  lastPlan?: { intents: string[] };
}
