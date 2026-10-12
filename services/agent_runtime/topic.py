"""
Topic guard: keeps the assistant on FreshCart customer service (input guardrail).

A deterministic semantic classifier, not an LLM: the local sentence-transformers model (the same one the RAG
Knowledge Base uses) embeds the customer's sentence and compares it with example sentences that are in scope
(FreshCart Rewards, FreshCart Online, FreshCart, answers to our questions) and out of scope (news, politics, public
figures, weather, sport, general knowledge, homework, coding, medical/legal/financial advice). A sentence is
blocked only when it is clearly closer to the out-of-scope examples.

It only ever judges questions and requests ("tell me…", "who is…", "what do you think…"); plain statements such
as card digits, a name, a date or a postcode are never checked, whether or not a question is pending.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass

log = logging.getLogger("topic")

MODEL = os.getenv("TOPIC_GUARD_MODEL", os.getenv("KB_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2"))
MIN_OUT = float(os.getenv("TOPIC_GUARD_MIN_SCORE", "0.36"))   # similarity to the nearest off-topic example
MARGIN = float(os.getenv("TOPIC_GUARD_MARGIN", "0.10"))       # …and this much closer than to any in-scope one

IN_SCOPE = [
    "What's my FreshCart Rewards points balance?", "How many points do I have?", "My points balance is wrong",
    "I didn't spend those points", "My balance is negative", "What did I spend my points on?",
    "Where is my online grocery order?", "When will my delivery arrive?", "Items were missing from my delivery",
    "I want a refund for my shopping", "My order was cancelled", "Can I change my delivery slot?",
    "How do I join FreshCart Rewards?", "I'd like a new FreshCart Rewards card", "I've lost my FreshCart Rewards card",
    "How long do bonus points take?", "Can I convert points to AeroMiles?", "What are my points worth?",
    "I want to speak to someone", "Can I talk to a person?", "I want to make a complaint about a FreshCart store",
    "What time does my local FreshCart close?", "Can I use my FreshCart Rewards card at HomeStore?",
    "Why did my points go down?", "Can you text me the link?", "What's Member Prices?",
    "Can you repeat that?", "Sorry, what did you say?", "Thanks, that's all", "Hello", "Is anyone there?",
    "Why do you need my date of birth?", "I don't know my card number", "Is this call recorded?",
]
OUT_OF_SCOPE = [
    "Tell me the latest news", "What's happening in the world today?", "What do you think of Donald Trump?",
    "Who is the prime minister?", "Who will win the election?", "Tell me about the war",
    "What's the weather tomorrow?", "Will it rain this weekend?", "Who won the football last night?",
    "What's the score in the cricket?", "Tell me a joke", "Write me a poem", "Sing me a song",
    "What's the capital of France?", "How far is the moon?", "Explain quantum physics",
    "Help me with my maths homework", "Write some Python code for me", "What's the meaning of life?",
    "Should I buy Bitcoin?", "What's the stock price of Apple?", "Give me some medical advice",
    "I have a headache, what medicine should I take?", "Can you give me legal advice?",
    "Who is Elon Musk?", "Tell me about Taylor Swift", "What's your opinion on immigration?",
    "Recommend me a film to watch", "What's a good holiday destination?", "Translate this into French",
    "Who are you voting for?", "What's the latest on the King?", "Tell me about Tesco's prices",
    "What's five times seven?", "What's twelve plus nine?", "Who is the president of America?",
    "Tell me about that politician", "Who is the richest person in the world?", "Who is the leader of the opposition?",
    "Tell me about a famous celebrity",
]
# Only questions and requests are judged; plain answers to our questions never are.
_ASKING = re.compile(r"\b(what|who|whom|when|where|why|how|which|tell me|explain|can you|could you|will you|"
                     r"do you|did you|is it|are you|give me|write|sing|recommend|thoughts|opinion|news|latest)\b|\?",
                     re.I)


@dataclass
class TopicVerdict:
    off_topic: bool
    score_out: float = 0.0
    score_in: float = 0.0
    nearest: str = ""
    skipped: str = ""


class TopicGuard:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._model = None
        self._in = self._out = None
        self.error: str = ""

    def load(self) -> bool:
        with self._lock:
            if self._model is not None:
                return True
            try:
                from sentence_transformers import SentenceTransformer
                try:
                    model = SentenceTransformer(MODEL, device="cpu", local_files_only=True)
                except Exception:  # noqa: BLE001 — first run: download once
                    model = SentenceTransformer(MODEL, device="cpu")
                enc = lambda xs: model.encode(xs, normalize_embeddings=True, show_progress_bar=False)  # noqa: E731
                self._in, self._out = enc(IN_SCOPE), enc(OUT_OF_SCOPE)
                self._model = model
                log.info("Topic guard ready (%d in-scope, %d out-of-scope examples)", len(IN_SCOPE), len(OUT_OF_SCOPE))
                return True
            except Exception as e:  # noqa: BLE001 — the guard is optional; the call must still work
                self.error = f"{type(e).__name__}: {e}"
                log.warning("Topic guard unavailable: %s", self.error)
                return False

    @property
    def ready(self) -> bool:
        return self._model is not None

    def check(self, text: str, answering: bool = False) -> TopicVerdict:
        words = re.findall(r"[A-Za-z']+", text)
        if len(words) < 3:
            return TopicVerdict(False, skipped="too short")
        if not _ASKING.search(text):
            # Plain statements (card digits, a name, a date, "I lost my card") are never judged: only questions and
            # requests can be off topic. Found live: "five six seven eight nine oh one" looked like maths.
            return TopicVerdict(False, skipped="answer to a pending question" if answering else "not a question")
        if not self.ready:
            return TopicVerdict(False, skipped="model not loaded")
        v = self._model.encode([text], normalize_embeddings=True, show_progress_bar=False)[0]
        s_in = (self._in @ v)
        s_out = (self._out @ v)
        i_out = int(s_out.argmax())
        best_in, best_out = float(s_in.max()), float(s_out[i_out])
        off = best_out >= MIN_OUT and best_out - best_in >= MARGIN
        return TopicVerdict(off, round(best_out, 3), round(best_in, 3), OUT_OF_SCOPE[i_out])


topic_guard = TopicGuard()
