// Positions on the 3D floor, as fractions of the floor size. The floor is viewed with
// rotateX(RX) rotateZ(RZ): (1,0) is the back corner, (0,1) the front corner.
import type { AgentKey, SystemKey } from "./types";

export const AGENTS: Record<AgentKey, { x: number; y: number; name: string; role: string; size: number }> = {
  coordinator: { x: 0.1, y: 0.62, name: "Coordinator", role: "Voice Live: hears, routes, speaks", size: 62 },
  orchestrator: { x: 0.48, y: 0.5, name: "Orchestrator", role: "Delegates · ID&V first", size: 66 },
  idv: { x: 0.5, y: 0.1, name: "ID&V", role: "Verifies the caller", size: 50 },
  rewards: { x: 0.86, y: 0.2, name: "FreshCart Rewards", role: "Points & transactions", size: 50 },
  online: { x: 0.9, y: 0.56, name: "FreshCart Online", role: "Orders & refunds", size: 50 },
  enrolment: { x: 0.76, y: 0.9, name: "Sign-up", role: "New members", size: 50 },
  knowledge: { x: 0.44, y: 0.92, name: "Knowledge", role: "RAG answers", size: 50 },
  escalation: { x: 0.14, y: 0.92, name: "Escalation", role: "Warm transfers", size: 50 },
};

export const AGENT_NAME = (k: string): string =>
  (AGENTS as Record<string, { name: string }>)[k]?.name ?? (k === "customer" ? "Customer" : k);

export const SUBAGENT_FOR_JOURNEY = (journeyId: string): AgentKey => {
  const w = journeyId.split(".")[0];
  return (["idv", "rewards", "online", "enrolment", "knowledge", "escalation"].includes(w) ? w : "orchestrator") as AgentKey;
};

// Business systems behind the MCP tools: what a presenter talks about (tool names stay in Engineer view).
export const SYSTEMS: Record<SystemKey, { name: string; what: string }> = {
  crm: { name: "CRM", what: "FreshCart Rewards accounts & identity" },
  online: { name: "Online Order System", what: "Orders & delivery" },
  taskdesk: { name: "TaskDesk", what: "Tasks & fraud cases" },
  contacthub: { name: "ContactHub", what: "Warm transfers" },
  sms: { name: "SMS", what: "Links by text" },
  rag: { name: "RAG Knowledge Base", what: "Policies & FAQs" },
};

const TOOL_SYSTEM: Record<string, SystemKey> = {
  validate_order: "online", check_caller_number: "online", verify_phone: "online", verify_postcode: "online", get_order_status: "online",
  create_task: "taskdesk", create_fraud_case: "taskdesk", warm_transfer: "contacthub",
  send_refund_link: "sms", send_link: "sms", search_knowledge: "rag", get_document: "rag",
};
export const systemOf = (tool: string): SystemKey => TOOL_SYSTEM[tool] ?? "crm";

// Human-readable action for the "Now happening" strip.
const TOOL_ACTION: Record<string, string> = {
  locate_account: "finding the account", verify_identity: "verifying identity", get_security_question: "fetching the security question",
  get_balance: "reading the points balance", get_transactions: "reading recent transactions", get_archive_transactions: "searching the archive",
  get_voucher: "opening voucher details", get_supplier_booking: "opening a partner booking", get_holders: "checking account holders",
  get_store: "looking up the store", set_reason_code: "protecting the account", deactivate_account: "deactivating the account",
  create_member: "creating the member", log_contact_outcome: "logging the outcome", validate_order: "checking the order number",
  check_caller_number: "comparing the calling number", verify_phone: "checking the phone number", verify_postcode: "checking the postcode",
  get_order_status: "reading the delivery status", create_task: "raising a specialist task", create_fraud_case: "raising a fraud case",
  warm_transfer: "connecting a colleague", send_refund_link: "texting the refund link", send_link: "texting the link",
  search_knowledge: "searching the knowledge base", get_document: "fetching approved wording",
};
export const actionOf = (tool: string): string => TOOL_ACTION[tool] ?? tool.replace(/_/g, " ");

export function point(key: string): { x: number; y: number } | undefined {
  return AGENTS[key as AgentKey];
}
