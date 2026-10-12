"""
The four functions the Voice Live model can call. This is where the LLM makes its decisions:
  route_request          → the coordinator decision: which journey(s) the customer wants
  submit_answer          → "this is the answer to the question we asked" (the pending question)
  transfer_to_colleague  → the customer wants a person
  end_call               → the customer is finished
Everything after the call is deterministic code: the Orchestrator delegates to the subagents, which run
the SOP journeys with their own scoped MCP tools and return structured facts for the model to speak.
"""
from __future__ import annotations

from datetime import date

from .config import PROMPTS_DIR
from .journeys.registry import ROUTABLE

MOODS = ["neutral", "positive", "confused", "anxious", "frustrated", "angry"]


# The mandated opening (client feedback, 5 Oct 2026). Spoken by the model, never pre-recorded.
WELCOME_LINE = "Welcome to FreshCart AI Assisted Customer Service."
GREETING_INSTRUCTIONS = (
    f'Open the call. Begin with exactly these words: "{WELCOME_LINE}" Then say that calls are recorded to make '
    "sure we're always offering the best possible service. Then, in one short sentence, ask how you can help "
    "today. Nothing else.")


def tool_definitions() -> list[dict]:
    journey_ids = [j.id for j in ROUTABLE]
    mood = {"type": "string", "enum": MOODS, "description": "The customer's mood right now."}
    return [
        {"type": "function", "name": "route_request",
         "description": "Call whenever the customer asks for something NEW. Choose every journey they asked for "
                        "(several if they asked for several things). Never use it to answer a pending question.",
         "parameters": {"type": "object", "properties": {
             "journeys": {"type": "array", "minItems": 1, "items": {"type": "object", "properties": {
                 "journey": {"type": "string", "enum": journey_ids},
                 "query": {"type": "string", "description": "For knowledge.ask: the question as a search query. "
                                                            "For escalation.human: the reason. Otherwise omit."}},
                 "required": ["journey"]}},
             "known_answers": {"type": "object", "additionalProperties": True,
                               "description": "Details the customer already gave in this same sentence, e.g. "
                                              "{\"order_number\": \"7004128855\"}, {\"card_number\": \"12345678901\"}, "
                                              "or a date they asked about as {\"target_date\": \"yyyy-mm-dd\"} "
                                              "(first day of the month if they named a month). Omit if none."},
             "customer_mood": mood},
             "required": ["journeys"]}},
        {"type": "function", "name": "submit_answer",
         "description": "Call when the customer answers the question you asked (the pending 'ask'). Use exactly "
                        "the field names from that 'ask' and follow its 'notes'. Put only what the customer actually "
                        "said; leave out fields they didn't answer.",
         "parameters": {"type": "object", "properties": {
             "answers": {"type": "object", "additionalProperties": True,
                         "description": "field name → value. Dates as yyyy-mm-dd, yes/no as true/false, "
                                        "numbers as digits, choices as the allowed value."},
             "customer_mood": mood},
             "required": ["answers"]}},
        {"type": "function", "name": "transfer_to_colleague",
         "description": "Call when the customer clearly asks to speak to a person, or agrees to be put through "
                        "because they need help with a FreshCart matter you can't handle. Give a clear reason: "
                        "it goes to the colleague with the call summary and transcript. Never call it after a "
                        "result said transferred: true.",
         "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
        {"type": "function", "name": "end_call",
         "description": "Call when the customer says they're finished, or when a result says close_call is true.",
         "parameters": {"type": "object", "properties": {}}},
    ]


def instructions(today: date | None = None) -> str:
    """System instructions for the coordinator: voice persona + routing rules + journey catalogue.
    Subagent domain notes are NOT here: each subagent's notes arrive only inside its own `ask`."""
    base = (PROMPTS_DIR / "voice_agent.md").read_text(encoding="utf-8")
    catalogue = "\n".join(f"- `{j.id}`: {j.description}" for j in ROUTABLE)
    today = today or date.today()
    return base.replace("{journey_catalogue}", catalogue).replace("{today}", f"{today:%A %d %B %Y} ({today.isoformat()})")
