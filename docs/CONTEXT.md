# CONTEXT.md — Callwright, Groq cascade

> FreshCart is a fictional retailer. All brands, systems, people, accounts and orders in this project are demo data.

Read this fully before changing anything. It is the source of truth for what the system is, why it is
built this way, what is verified, and what is left to do.

---

## 1. What this is

A production-shaped voice AI agent for FreshCart contact centre, covering FreshCart Rewards (points balance journeys,
new card sign-up) and FreshCart Online (order verification, delivery status, refund query). The journeys are
encoded from SOP flowcharts.

**Voice is a cascade on Groq (since 10 Oct 2026; it replaced Azure Voice Live speech to speech).** Per call,
`cascade.py` runs:
- local **Silero VAD**: turn detection and barge-in (browser AEC + mic gate handle echo; no server AEC)
- **Groq Whisper** (`whisper-large-v3-turbo`): speech to text, one request per customer utterance
- **Groq LLM** (`openai/gpt-oss-120b`, reasoning effort low): the coordinator, streamed, with function calling
- **Groq Orpheus TTS** (`canopylabs/orpheus-v1-english`, voice `hannah`): one request per sentence (≤200 chars),
  synthesised a few sentences ahead of playback and played in order

`cascade.py` exposes the same realtime event protocol `call.py` used with Voice Live (session.update,
input_audio_buffer.*, conversation.item.*, response.*), so the bridge's guardrails, merging, echo handling,
post-ack pause and silence logic are unchanged.

**Thesis: the AI understands and speaks; deterministic code decides.** coordinator → Orchestrator → subagents
→ MCP → backend:

| Architecture box | Implementation |
|---|---|
| Speech to text | Silero VAD + Groq Whisper (`cascade.py`) |
| Coordinator, Reply Writer | Groq LLM with the 4 functions (`tools.py`): routes, extracts answers, reads mood, gives the instant ack, writes the reply |
| Voice | Groq Orpheus TTS, sentence by sentence (`cascade.py`) |
| Input / output guardrails | `guardrails.py`, around the cascade (`call.py`). Output rails check each sentence *before* it is synthesised |
| Orchestrator | `orchestrator.py`: delegates to subagents, ID&V first (once per call, shared), parallel journeys, chaining, the pending question |
| Subagents (ID&V, FreshCart Rewards, Online, Sign-up, Knowledge, Escalation) | `subagents.py`: **deterministic workflow subagents**, one per subagent per call, each with its own journey runs, its own least-privilege MCP toolbox and its own domain notes |
| MCP tool servers → backend systems | `mcp_gateway/` → `mock_backend/` (incl. the Chroma RAG Knowledge Base) |

**Where an LLM is used, and where it isn't (decided 30 Sep 2026).** The only chat LLM is the coordinator. Every SOP step
is a tool call, a rule (diamond), a fact to tell or a question to ask. The only language work is turning an
answer into a typed value, and the coordinator already hears the answer, so it proposes the value in
`submit_answer` and code validates it (normalizers; card/order numbers re-read from the transcript by regex).
A per-subagent LLM would add 0.7–2 s per answer turn for no accuracy gain (it would only see the transcript,
while the coordinator heard the audio). The cascade needed a subagent AI only because its coordinator saw masked
text. Rule for the future: a subagent gets a small model **only** for a step the coordinator can't do in one hop
(open-ended capture, reasoning over large data). The hook for that is `Subagent` in `subagents.py`.
The Knowledge subagent's retrieval uses an **embedding model** (sentence-transformers), not a chat LLM.

### Non-negotiable rules
- No hardcoded responses and nothing faked. The model speaks everything, from structured facts.
- Business tools are reached only through MCP. The mock backend is a system of record, not AI output.
- Tests never simulate an LLM. They call the orchestrator's tool handlers with the arguments a model would send,
  against the real engine, the real MCP gateway and the real backend.
- Prompts are readable markdown in `/prompts`.

---

## 2. Architecture

```
Caller (browser today; ContactHub/ACS telephony later)
  │  WS /ws: PCM16 24 kHz mono up, JSON events + PCM16 audio down
  ▼
Agent runtime (FastAPI :8000)   services/agent_runtime
  call.py  CallBridge ── one CascadeEngine per call (cascade.py) ──► COORDINATOR (Groq LLM)
     │  • audio → Silero VAD → speech_started (barge-in) / speech_stopped → Groq Whisper → transcript
     │  • transcript → INPUT GUARDRAILS → response.create   (GUARD_BEFORE_RESPONSE)
     │  • Groq chat stream: text + function calls → Orchestrator → function_call_output → response.create
     │  • each finished sentence → OUTPUT GUARDRAILS → (pass) Groq Orpheus TTS → audio deltas → caller
     │    (violation) response.cancel + rephrase, before any audio of that sentence exists
     ▼
  orchestrator.py  route / answer / transfer / end_call → delegates (parallel, ID&V first, chaining)
     ▼
  subagents.py  ID&V · FreshCart Rewards · Online · Sign-up · Knowledge · Escalation   (deterministic subagents, one per call)
     │  each runs its SOP journeys (journeys/) through its own ScopedToolbox; returns structured facts
     │  MCP (streamable HTTP)
     ▼
MCP gateway (FastMCP :8200): /member_identity /order_verification /loyalty_accounts /order_service
                             /case_management /member_enrolment /knowledge_base
     │  REST
     ▼
Mock enterprise backend (FastAPI :8100): CRM, Order System, TaskDesk, ContactHub, SMS Gateway,
                                         RAG Knowledge Base (Chroma + sentence-transformers, rag.py)
```

### The four functions the model can call (`tools.py`)
| Function | The LLM decision it represents | What code then does |
|---|---|---|
| `route_request(journeys[], known_answers?, customer_mood?)` | coordinator: which journey(s) the customer wants | validates ids (unknown, running and duplicate dropped), starts journeys in parallel, inserts ID&V, pre-fills from `known_answers` + transcript |
| `submit_answer(answers, customer_mood?)` | "this answers the pending question", plus the coordinator's reading of the answer | validates every value (normalizers), card/order numbers re-read from the transcript by code, hands it to the subagent that asked, which resumes its journey |
| `transfer_to_colleague(reason)` | customer wants a person, or agreed to one for a matter the assistant can't handle | **consent checked by code** (`wants_a_person`: they asked for a person, or said yes right after an offer) → escalation journey → ContactHub warm transfer with the handover package |
| `end_call()` | customer is finished (or `close_call: true`) | outcome + disposition logged to CRM; call closes after the goodbye. Refused after a transfer |

Every tool returns `{facts, ask, outcomes, close_call}` (+ `transferred: true` after a warm transfer: the colleague
takes over, so there is no goodbye and no further question). The model speaks the facts and asks exactly the `ask`.
The `ask` carries the asking subagent's name and its domain notes (`prompts/subagent_<key>.md`); those notes are
not in the global instructions, so each subagent's guidance reaches the coordinator only while that subagent asks.
Details given before we ask ("it's 7004128855", "in March") go in `known_answers`, are kept on the journey
run, and pre-fill its first question even when the journey only starts after ID&V.

### Least privilege (enforced by MCP URL, not prompts)
| Subagent | Journeys | MCP scopes |
|---|---|---|
| ID&V | FreshCart Rewards standard, FreshCart Rewards enhanced, Online order check | member_identity, order_verification, case_management |
| FreshCart Rewards | view balance, balance lower than expected, negative/fraud, history | loyalty_accounts, case_management, knowledge_base |
| Online | delivery status, refund query | order_service, case_management, knowledge_base |
| Sign-up | register a new card | member_enrolment, knowledge_base |
| Knowledge | FAQ answers (RAG Knowledge Base) | knowledge_base |
| Escalation | speak to a colleague | case_management |

---

## 3. Code map

```
services/agent_runtime/
  config.py        all settings from env (.env loaded automatically): VoiceSettings, ContentSafetySettings, Settings
  azure_auth.py    Entra ID or API key for Azure AI Content Safety (optional); token cache
  cascade.py       CascadeEngine: Silero VAD → Groq Whisper → Groq LLM → Groq Orpheus, behind the realtime event
                   protocol; session.update builder (only module that knows Groq's API)
  tools.py         the coordinator: 4 function definitions + instructions (voice_agent.md, journey catalogue, today)
  call.py          CallBridge: one call; events, response gating, tool dispatch, guardrails, progress, barge-in
  orchestrator.py  tool handlers → subagents (parallel, ID&V gating, prefill, chaining, end-call), answer
                   validation (finders first, incomplete vs wrong, answers joined across turns, contradictions),
                   transfer consent, outcome logging
  subagents.py       Subagent: deterministic subagent (own runs, ScopedToolbox, notes); attaches the handover package
                   to warm transfers; a 5xx/timeout is a SystemUnavailable (never "not found")
  outcomes.py      call disposition + the colleague's handover package (reason, summary, redacted transcript)
  topic.py         topic guard: local embedding classifier (questions/requests only) keeping calls on FreshCart
  guardrails.py    input: rules, topic, Azure AI Content Safety (Prompt Shields, harm); output: grounding, secrets,
                   PII read-back, card digits, commanding phrasing, goodbye after a transfer
  session.py       per-call state (vault, idv, runs, history, last transcript, grounding allow-list)
  pii.py · normalizers.py · mcp_hub.py · events.py
  journeys/        dsl, engine, slots, idv, rewards, enrolment, online, general, registry  (unchanged)
  app.py           FastAPI: /ws, /api/*
prompts/voice_agent.md (coordinator) + subagent_*.md (each subagent's notes, sent inside its own ask)
services/mock_backend/rag.py   RAG Knowledge Base: Chroma (data/chroma) + all-MiniLM-L6-v2, rebuilt on content change
scripts/run_all.ps1 · stop_all.ps1   Windows run/stop (logs/); run_all.sh for Linux
scripts/call_sim.py  live call simulator: SAPI speech → /ws → real Groq cascade; transcripts in recordings/
scripts/scenarios.py 40+ live end-to-end scenarios (typed or spoken) with universal safety checks → logs/test_results.jsonl
deploy/azure-vm/   Ubuntu VM only: setup.sh, systemd units, nginx (TLS + WebSocket upgrade)
```

---

## 4. Voice configuration (Groq)

| Setting | Default | Notes |
|---|---|---|
| `GROQ_API_KEY` | — | required; console.groq.com → API Keys |
| `GROQ_LLM_MODEL` | `openai/gpt-oss-120b` | needs function calling. gpt-oss has no parallel tool calls (not needed: `route_request` takes several journeys). Faster/weaker: `llama-3.3-70b-versatile` |
| `GROQ_REASONING_EFFORT` | `low` | sent only for gpt-oss models (with `include_reasoning=false`) |
| `GROQ_STT_MODEL` | `whisper-large-v3-turbo` | `verbose_json`; segments with `no_speech_prob ≥ STT_NO_SPEECH_MAX` (0.6) are dropped; `STT_PROMPT` biases vocabulary |
| `GROQ_TTS_MODEL` / `GROQ_TTS_VOICE` | `canopylabs/orpheus-v1-english` / `hannah` | **preview model** on Groq; WAV only, 200 chars per request; US voices only (autumn, diana, hannah, austin, daniel, troy) |
| Turn detection | Silero VAD, `VAD_THRESHOLD` 0.5, `VAD_MIN_SPEECH_MS` 160, `VAD_SILENCE_MS` 500 | `create_response` = not `GUARD_BEFORE_RESPONSE`. While a long-input question is pending, `silence_duration_ms` is raised to `VAD_SILENCE_MS_LONG_INPUT` (1000) by a mid-session `session.update`, then restored |
| Audio | PCM16 24 kHz mono both ways | TTS WAV is resampled to 24 kHz; VAD runs at 16 kHz |
| Content Safety | off unless `AZURE_CONTENT_SAFETY_ENDPOINT` is set | Prompt Shields + Hate/Sexual/Violence/SelfHarm (self-harm flags, never silently blocks) |

## 5. SOP interpretation decisions

1. **J2 red points / dormancy order** — implemented as: red points? → yes → dormant? (yes: dormancy process;
   no: explain deductions); no → pending? (yes: advise; no: archive request task on TaskDesk).
2. **J3 "Recognise transaction?"** — implemented literally from the chart (yes → product-specialist task;
   no → explain delay and reassure). It reads possibly inverted.
3. **J3 "Store colleague?"** — implemented as a question to the caller (are you a FreshCart store colleague?).
   The chart doesn't say whether this is asked or looked up.
4. **J3 "Store is not local?"** — store region ≠ the verified holder's region (data decision).
5. **J3 fraud threshold** — `> 2000` points (`FRAUD_POINTS_THRESHOLD`).
6. **"Voice contact?" diamonds** — `session.channel == "voice"` (`CONTACT_CHANNEL`).
7. **Online redemption, still disputing** → same fraud routing as non-local in-store (warm transfer, then
   fraud case if the line is unavailable).
8. **ID&V retries** — 1 retry then refuse (`IDV_MAX_RETRIES`). The customer is never told which detail failed.
   Enhanced retry re-asks address/secret/word plus only the name/DOB fields that mismatched.
9. **Memorable word** — full word (partial-character mode not implemented).
10. **Lost/stolen card during ID&V** — warm transfer to a lost/stolen queue (chart says "see process").
11. **Sign-up duplicate check** — the backend returns 409 for an existing member; not on the chart.
12. **Marketing consent** — asked only if email/mobile was given; otherwise "leaves blank and unticks".

---

### Online orders IVR flowchart ("Refund Query: Customer-IVR journey")
13. **Keypad → conversation.** "Press 1/2" menus and "enter digits after the beep" become questions the
    customer answers naturally. The Online main menu (message 8) is replaced by the coordinator's intent routing.
14. **IDV 1 read-back.** The order number is read back digit by digit and confirmed (message 5). "No" →
    re-enter; the flowchart's "Max 2 attempts" is honoured via `IDV_MAX_RETRIES=1` (two attempts total).
15. **"Is Order Number Valid?" No after attempt 2** → transfer to Online human agent (the standard IVR
    pattern).
16. **IDV 2 phone** is asked only when the calling number (ANI) differs from the order's number, as the flowchart
    says. The backend compares numbers and never returns the stored number. In the web demo, ANI comes from
    the "Caller ID" field. A phone mismatch gets one retry, then transfer (retry count not on the flowchart).
17. **IDV 3 postcode "No".** The flowchart routes to "Play Online IVR Menu" (i.e. unverified → menu → agent). In
    conversation there's no menu, so after one retry the call transfers to an online orders agent.
18. **Delivered + "issue with the items"** (message 10) continues into the Refund Query journey via
    `End(chain="online.refund_query")` rather than duplicating nodes.
19. **Refund Query** follows the Technical Help lane: deflection message (messages 1 & 9, demo wording in
    `KB-ONLINE-REFUND-SELFSERVE`) → "Does customer need link?" → Refund SMS (Message Broker → SMS Gateway)
    to the phone on the order → "Anything else?". The deflection also offers a colleague; the customer can
    ask for one at the link question (`wants_agent` escape → ContactHub transfer).
20. **"End Call"** nodes set `end_call=True`; the orchestrator closes the call after a warm goodbye, but only
    if no other journey is still open. "Anything else? Yes" hands back to the coordinator (the flowchart loops back
    to the menu).
21. **Refunds are not raised in-call.** The AS IS process deflects to online self-service. A TO-BE in-call
    refund (Order System refund API, eligibility rules) would be a new journey once refund eligibility rules are defined.
22. **"Late delivery or on the day query option unable to replicate"** is noted on the flowchart and not modelled.


## 5b. Conversation design changes (5 Oct 2026)

1. **Natural, never commanding.** Slot `description`s say only what a value is; formats live in an internal
   `parse_hint`; the prompt forbids format instructions and marks all `ask` fields as internal. An output rail
   (`commanding`) stops "say yes or no", "digits only", "in the format…", "19-digit…". Code parses natural input:
   dates of birth in any UK form (two-digit-year pivot, past only), postcodes with spelled letters / NATO words,
   phone numbers with +44 / "oh" / "double" / "triple", grouped and hesitant digits, many yes/no forms. Finders read
   the transcript first; the model's value is the fallback.
2. **Incomplete ≠ wrong.** A partial card/order/phone/postcode/address is `invalid.kind = "incomplete"`: nothing is
   stored or verified, no retry is used, the agent says "no rush" and the next fragment is joined to it. Long-input
   questions get a longer end-of-turn silence and split transcripts are merged before guardrails and tools.
3. **Pause after acknowledgements.** The answer waits until the ack/progress line has finished playing (estimated
   from PCM bytes) plus `POST_ACK_DELAY_MS`; barge-in cancels the wait; reported in `turn.done`.
4. **Welcome line** "Welcome to FreshCart AI Assisted Customer Service." is spoken first (model-spoken).
5. **Transfers**: no goodbye (prompt + output rail + `end_call` refused after a transfer); fraud transfers say only
   "transfer to our fraud team, they'll help, please stay on the line"; `DEMO_CUT_CALL_ON_TRANSFER` closes the demo
   session after the message has played. The colleague gets a handover package: reason, summary, redacted transcript.
6. **Dispositions** (`outcomes.py`): automated · deflected · transferred_to_advisor · partial_automation ·
   partial_deflection · failed_idv · abandoned, logged once per call with the transfer reason and per-journey
   outcomes (also on hang-up, silence and Voice Live disconnect); shown in the UI call summary and Engineer view.
7. **Merge flow**: `rewards.balance.view` with a negative balance tells it plainly and chains into
   `rewards.balance.negative`; the enhanced step-up reuses card, name and DOB (only address, security answer and
   memorable word are asked). A contradictory dispute answer gets one clarifying question (`Slot.conflict`).
8. **UI**: light theme (dark kept), queued handoffs with a 1.2 s dwell, a "Now happening" strip, uninvolved agents
   dimmed, a Systems panel in business terms, a replay mode (`?replay=<scenario>`).
9. **Scope and escalation**: a topic guard keeps calls on FreshCart; FreshCart matters the assistant can't
   handle are offered to a colleague; transfer consent is checked by code.

Bugs found and fixed while testing these: optional escape fields (`card_lost`, `wants_agent`) were never offered to
the coordinator, so "I've lost my card" and "I'd rather speak to someone" at the refund offer couldn't work live;
a CRM outage on `locate_account` read as "account not found" and used the customer's retry; the topic guard judged
digit strings as maths questions when no question was pending; Voice Live rejects echo cancellation on a text-only
session. Details: `docs/TEST_REPORT.md`.

## 5c. Live demo hardening (8 Oct 2026)

1. **Self-interruption (agent hearing its own voice on speakers).** Chrome's echo canceller ignores plain Web Audio
   output, so the agent's voice leaked into the mic, triggered barge-in and was answered as a customer turn. Now:
   playback goes through a local WebRTC loopback so browser AEC sees it (`?loopback=0` disables); a mic gate sends
   silence while the agent is audible unless speech is clearly louder than the measured echo (`?gate=0` disables);
   the runtime drops transcripts that only repeat the agent's last 20 s of speech (`echo.ignored`, item deleted) and,
   if that cut the agent off, asks it to pick up where it stopped (`resume`). Short untranscribable blips no longer
   trigger "could you repeat that" (`noise.ignored`).
2. **Long-input lag.** `answer.format` event: a complete card/order/phone number or postcode is processed at once;
   a partial one waits `TRANSCRIPT_MERGE_WINDOW_INCOMPLETE_MS` (3000) for the rest; security answers keep the normal
   window. Long-input VAD silence 1500 → 1000 ms. Fragments are joined without the transcriber's full stops (they
   split "1234567." + "8901." into two numbers, so the finder failed and only the model's value saved it). Live:
   card turn 4.99 s → 2.23 s speech-end-to-audio.

## 6. PII, safety and emotional voice

- The coordinator LLM (Groq) sees the Whisper transcript, including personal details the customer says; customer
  audio goes to Groq for transcription. Masking applies to everything the runtime emits, logs or stores. Everything the runtime emits or
  stores is redacted (`pii.Redactor`). When the pending question asks for personal details, the UI and
  history show `⟨customer gave personal details: …⟩` instead of the words.
- The backend never returns stored secrets (identity checks return match flags only).
- **Input guardrails** run on the transcript **before** the model may reply (`GUARD_BEFORE_RESPONSE=true`,
  achieved with `turn_detection.create_response=false`). A blocked utterance is deleted from the conversation
  and the model is asked to decline politely.
- **Output guardrails** check the model's transcript sentence by sentence as it streams: ungrounded numbers,
  full card numbers, repeated security answers, the caller's DOB/postcode/phone/address read back, commanding
  phrasing, and a goodbye after a transfer → `response.cancel`, playback cleared, and the model is
  told to rephrase.
- **Topic guard** (input): news, politics, public figures, weather, general knowledge… are declined politely before
  the model replies. Only questions and requests are judged (never card digits, names or dates). Text usually streams ahead of audio, but a fragment may already have been heard; this is
  documented as a known limitation.
- **Emotional voice:** Azure HD voices infer emotion and prosody from the text. The instructions tell the model
  to match the customer's mood, and `customer_mood` is captured on every tool call. Temperature and rate are
  set by config.

## 7. Events (runtime → UI)
`session.ready, voice.ready, turn.started, guardrail, agent.state, coordinator.plan, coordinator.dropped_intent,
sentiment, handoff, handoff.return, journey.node, journey.status, tool.call, tool.result, subagent.extracted,
assistant.say (text), audio.delta (base64 PCM16 24 kHz), audio.clear (barge-in), turn.done (first_audio, tools,
post_ack_delay_ms), vad.mode, vad.updated, turn.merged, post_ack_pause, silence, handover.card (reason, summary,
transcript), call.summary (disposition, transfer_reason, journeys, ended_by), call.ended (reason), error`

## 5d. Groq cascade (10 Oct 2026)

Voice moved from Azure Voice Live speech to speech to a cascade on Groq (see §1, §4). `voicelive.py` was replaced
by `cascade.py`; `call.py`, the Orchestrator, subagents, journeys, guardrails and UI are unchanged apart from
names (`settings.voice`, outcome `voice_error` / `voice_closed`). `prompts/voice_agent.md` gained a "Speech in,
speech out" section (the coordinator reads a transcript; its words are spoken by TTS: plain short sentences).
`tests/test_cascade.py` replays Groq's wire format (SSE chat chunks, WAV, transcription JSON) and runs Silero
VAD on a recorded call.

**Verified live on Groq's free tier (10 Oct 2026):** a full FreshCart Rewards balance call (typed customer turns, spoken
agent): greeting → route → card → name/DOB → standard ID&V passed → balance 3,820 read out → disposition
`automated`; Orpheus returns 24 kHz PCM16 WAV. Silero VAD + Whisper transcribe a recorded call accurately.
Found live and fixed: with `tool_choice: none` gpt-oss still calls a tool when the history has tool calls
(Groq rejects it), so no-tool replies get earlier tool exchanges as plain notes.

**Free tier limits** (per model): LLM 8,000 tokens/min (one request ≈ 2.5k+ tokens, so turns wait 5–15 s on
429s; `GROQ_LLM_FALLBACK_MODELS` switches model at once on a 429); Orpheus 100 requests/day and 1,200
characters/min (the first sentence of a reply is synthesised alone, later ones packed up to 200 characters).
Fine for a demo; use Groq's Developer tier for natural pacing.

## 8. Status

**7 Oct 2026:** 179 deterministic tests pass (no Azure, no fake LLM; a Voice Live *transport* stub replays protocol
events for the call-timing tests). The live scenario results, dispositions and the worst-case analysis are in
`docs/TEST_REPORT.md`. Generated reports, `logs/` and `recordings/` are kept out of the public repository
(`.gitignore`); regenerate them with `scripts/scenarios.py` and `scripts/call_sim.py`.

| Area | Status (30 Sep 2026) |
|---|---|
| Journeys, MCP scopes, backend (29 engine tests) | ✅ passing |
| Orchestrator + subagents: routing, pending answers, shared ID&V, prefill across ID&V, chaining, close, subagent isolation, vector RAG (9 tests) | ✅ passing |
| Real Voice Live session on an Azure AI Foundry resource (`gpt-realtime` datazone, `en-GB-Ada:DragonHDLatestNeural`, API key) — retired, see §5d | ✅ **verified live** |
| Live flow: Example 2, parallel FreshCart Rewards (balance + March history, one ID&V, answered together, close), typed and spoken | ✅ passed; spoken card digits and DOB captured correctly |
| Live flow: Examples 4+3+5, Online (order number pre-filled → read-back, phone skipped by caller ID, FAQ asked while the postcode was pending → answered from RAG → postcode re-asked, delivered → missing items → chained refund query → SMS to ••0111 → close), spoken | ✅ passed |
| Event names | `response.audio.delta` / `response.audio_transcript.*` are what Voice Live sends here; the `output_audio` aliases are kept for newer models |
| First-audio latency (speech end → first audio), live | 1.5–2.8 s with `GUARD_BEFORE_RESPONSE=true` and mock backend latency 120–450 ms per call |
| Azure AI Content Safety calls | ⚠️ untested (optional; the rule checks run) |
| Frontend streaming mic, PCM playback, barge-in | ✅ builds and is served at :8000 · ⚠️ needs a person to test in a browser |

## 9. Run

**Windows VM (current dev box, no Docker):** Python 3.12 in `.venv` (installed with `uv`), Node 24, VC++ runtime (needed by torch).
`.env` holds the endpoint and key (never commit it; `.env.example` stays blank). `.\scripts\run_all.ps1` builds the UI if needed
and starts backend, gateway and runtime (logs in `logs\`); open http://localhost:8000 on the VM. `.\scripts\stop_all.ps1` stops them.
Tests: `.venv\Scripts\python.exe -m pytest -q` (no Azure). Live scenarios: `.venv\Scripts\python.exe scripts\scenarios.py [names] [--audio-set]`
(typed scenarios use a text-only session: cheaper; `--audio-set` runs the spoken ones). Single demo calls:
`scripts\call_sim.py online|rewards|fraud|safety [--text]`. Replay a recorded call in the UI: `http://localhost:8000/?replay=<scenario>`.

**Linux:** `pip install -r requirements.txt`, `cd frontend && npm install`, `cp .env.example .env`, `./scripts/run_all.sh`.

**Azure VM (Ubuntu):**
1. Enable a system-assigned managed identity on the VM. On the Foundry resource: IAM → Cognitive Services User + Foundry User.
2. Open ports 80/443 in the NSG, and point a DNS name at the VM (the browser mic needs HTTPS).
3. Run `sudo bash deploy/azure-vm/setup.sh your.domain`. This installs everything, starts 3 systemd services and sets up nginx + certbot.
4. Edit `.env` (the endpoint at minimum), then `sudo systemctl restart callwright-runtime`. Logs: `journalctl -u callwright-runtime -f`.

## 11. Demo accounts

Customers may read the full 19-digit card (`60012300` + last 11) or just the last 11.

| Card (last 11) | Caller | DOB | Address / postcode | Security Q → A | Memorable word | Scenario |
|---|---|---|---|---|---|---|
| 12345678901 | Priya Sharma | 12 Apr 1988 | BS6 6PR | first school → St Michaels | lantern | J1 balance 3,820; J4 vouchers (Mar 2026 DineClub, 2019 AeroMiles archive) |
| 20456789012 | Oliver Bennett | 3 Nov 1979 | SW3 4TR | mother's maiden → Hartley | copper | J2 deductions (TV return) |
| 30567890123 | Margaret Doyle | 27 Feb 1954 | LS17 6AA | first pet → Biscuit | harbour | J2 dormant |
| 40678901234 | James Okafor | 19 Jul 1992 | M3 4LZ | born city → Lagos | meridian | J2 pending points |
| 50789012345 | Aisha Rahman | 30 Sep 1985 | G12 8TN | nickname → Sunny | thistle | J2 archive request |
| 60890123456 | Tom Hughes | 8 Jan 1990 | 5 Gloucester Road BS7 8AA | first car → Fiesta | orchard | J3 non-local in-store 2,500 → fraud transfer (toggle fraud line off → fraud case) |
| 70901234567 | Sophie Clarke | 15 Dec 1996 | N1 0NP | favourite film → Amelie | velvet | J3 local in-store 1,800 → police |
| 80012345678 | David Evans (primary) | 21 May 1975 | LS6 2BB | first job → Paperboy | granite | J3 online redemption → household check |
| 80012345678 | Rachel Evans (secondary) | 9 Oct 1977 | LS6 2BB | street grew up → Elm Street | saffron | J3 non-primary → colleague? → deactivate / protect |
| 90123456789 | Ben Walsh | 4 Mar 1983 | BS1 5LJ | primary teacher → Mrs Patel | falcon | J3 not disputed → recognise? |

Sign-up: any new name/DOB/address. Priya's details trigger the duplicate branch.

### FreshCart Online demo orders
Set **Caller ID** (top bar) to the order's phone to skip IDV 2; leave it blank or use another number to hear the phone check.

| Order number | Customer | Phone on order | Postcode | Status | Scenario |
|---|---|---|---|---|---|
| 7004128855 | Hannah Moore | 07700900111 | SE1 7PB | delivered 26 Sep | "items missing" → refund deflection → SMS link |
| 7004133907 | Daniel Price | 07700900222 | BS8 1TH | not delivered | due Fri 2 Oct, 5–6pm → agent? |
| 7004120016 | Grace Adeyemi | 07700900333 | M20 2WS | cancelled | → wants agent → ContactHub transfer |

Any other 10-digit number fails IDV 1 (two attempts, then transfer).

---


## 12. Task list for Claude Code (in order)
1. ~~First live session~~ and ~~event names~~: done 30 Sep 2026 (see Status).
2. **Browser test** at http://localhost:8000 on the VM: real mic, barge-in, captions, the live floor.
3. **Function calling quality.** Examples 2–7 pass live. The full manual test plan with demo data and expected
   results is `docs/TEST_SCRIPT.md`. Tune `prompts/voice_agent.md` and the tool descriptions from its findings.
   Fixed after live runs: double goodbye; a second transfer after a fraud transfer (a transfer now closes the
   call); the Example 7 phrasing slipping past the rule patterns.
   Seen live: semantic VAD sometimes ends a turn at a mid-sentence pause ("Actually, quick question before that.");
   the model now waits instead of routing an empty request. If it's frequent with real callers, try `AZURE_VAD_SILENCE_MS=700`.
4. **Latency.** `turn.done.first_audio`. Levers: `GUARD_BEFORE_RESPONSE` (off = faster), `AZURE_VAD_SILENCE_MS`,
   and the model choice (`gpt-realtime-mini`).
5. **Voice.** Try `en-GB-Ada:DragonHDLatestNeural` versus `en-GB-Ollie:DragonHDLatestNeural`, and `AZURE_VOICE_TEMPERATURE`.
6. **Content Safety.** Create the resource, set the endpoint, and give the VM identity Cognitive Services User.
7. **Telephony.** For real phone calls, bridge ContactHub or Azure Communication Services media streaming into
   `CallBridge.audio()` (8/16 kHz input needs resampling or `input_audio_sampling_rate=16000`).
8. **Production hardening:** Key Vault for any keys, Azure Monitor / OpenTelemetry (the `azure-ai-voicelive` SDK
   has a tracing instrumentor), a real vector index for the RAG Knowledge Base, and persistence for the mock
   systems replaced by the real APIs.
9. **Review the SOP interpretation decisions** in section 5.

## 13. Known limitations
- The topic guard is an embedding classifier calibrated on ~75 examples; it can miss unusual off-topic phrasings
  (the coordinator prompt is the second line of defence) and only judges questions/requests.
- Transcript merging only applies while a long-input question is pending and `GUARD_BEFORE_RESPONSE=true`.
- The post-ack pause uses an estimate of when the browser finishes playing (PCM bytes from first audio).
- Output guardrails act while speech streams, so a fragment may be heard before a cancel.
- One call equals one CascadeEngine; there is no resume after a disconnect.
- Orpheus TTS is a Groq preview model, with US voices only. Each sentence is a separate request, so first audio
  waits for the first full sentence. The cascade has no server-side echo cancellation or noise suppression
  (browser AEC, the mic gate and transcript echo filtering remain). Barge-in truncation of history is by
  sentence, not by the audio position actually played.
- The in-memory backend resets on restart (`/admin/reset`).
