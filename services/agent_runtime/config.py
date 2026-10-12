"""
Single source of configuration. Every value comes from the environment (.env locally,
App Settings / Key Vault references on Azure), so nothing environment-specific is in code.

Voice is a cascade on Groq: speech to text (Whisper) → LLM coordinator (function calling) →
text to speech (Orpheus), with local voice activity detection (Silero VAD).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in ("1", "true", "yes")


ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = ROOT / "prompts"
load_dotenv(ROOT / ".env")  # real environment variables win over .env


@dataclass(frozen=True)
class VoiceSettings:
    """The cascade: voice activity detection (local Silero VAD) → speech to text → LLM (coordinator, function
    calling) → text to speech. STT, LLM and TTS run on Groq (OpenAI-compatible API)."""
    groq_api_key: str = _env("GROQ_API_KEY")
    groq_base_url: str = _env("GROQ_BASE_URL", "https://api.groq.com/openai/v1")

    # LLM = the coordinator. Must support function calling.
    model: str = _env("GROQ_LLM_MODEL", "openai/gpt-oss-120b")
    # Tried in order when the model is rate limited (each Groq model has its own per-minute token budget)
    fallback_models: tuple[str, ...] = tuple(m.strip() for m in _env("GROQ_LLM_FALLBACK_MODELS",
                                                                      "openai/gpt-oss-20b").split(",") if m.strip())
    reasoning_effort: str = _env("GROQ_REASONING_EFFORT", "low")   # gpt-oss / qwen only; empty = not sent
    model_temperature: float = float(_env("LLM_TEMPERATURE", "0.7"))
    llm_timeout_s: float = float(_env("LLM_TIMEOUT_S", "20"))

    transcription_model: str = _env("GROQ_STT_MODEL", "whisper-large-v3-turbo")
    transcription_language: str = _env("STT_LANGUAGE", "en")
    # Vocabulary hint for Whisper (names it would otherwise mishear)
    transcription_prompt: str = _env("STT_PROMPT", "FreshCart, FreshCart Rewards card, FreshCart Online, postcode, "
                                                   "memorable word, order number.")
    # A segment Whisper itself thinks is probably not speech is dropped (stops "Thank you." on noise)
    no_speech_max: float = float(_env("STT_NO_SPEECH_MAX", "0.6"))

    tts_model: str = _env("GROQ_TTS_MODEL", "canopylabs/orpheus-v1-english")
    voice_name: str = _env("GROQ_TTS_VOICE", "hannah")     # autumn diana hannah · austin daniel troy
    tts_max_chars: int = int(_env("GROQ_TTS_MAX_CHARS", "200"))   # Orpheus limit per request
    tts_parallel: int = int(_env("TTS_PARALLEL", "3"))            # sentences synthesised ahead of playback

    vad_threshold: float = float(_env("VAD_THRESHOLD", "0.5"))
    vad_min_speech_ms: int = int(_env("VAD_MIN_SPEECH_MS", "160"))   # speech this long starts a turn (barge-in)
    vad_silence_ms: int = int(_env("VAD_SILENCE_MS", "500"))
    # While the customer reads out digits, an address or a security answer, wait longer before ending their turn
    # (sent as a mid-session session.update, restored afterwards).
    vad_silence_ms_long_input: int = int(_env("VAD_SILENCE_MS_LONG_INPUT", "1000"))
    # Transcripts arriving within this window of each other (long-input questions only) are one answer.
    # An answer already complete in format (a full card / order / phone number, a full postcode) doesn't wait;
    # a partial one waits longer for the rest.
    merge_window_ms: int = int(_env("TRANSCRIPT_MERGE_WINDOW_MS", "1500"))
    merge_window_incomplete_ms: int = int(_env("TRANSCRIPT_MERGE_WINDOW_INCOMPLETE_MS", "3000"))

    # True: we check every customer utterance with guardrails BEFORE the model may reply
    # (turn_detection.create_response = false). False: faster, guardrails run alongside.
    guard_before_response: bool = _bool("GUARD_BEFORE_RESPONSE", True)

    @property
    def configured(self) -> bool:
        return bool(self.groq_api_key)


@dataclass(frozen=True)
class ContentSafetySettings:
    # Azure AI Content Safety (optional). Empty endpoint = only the built-in rule checks run.
    endpoint: str = _env("AZURE_CONTENT_SAFETY_ENDPOINT")
    api_key: str = _env("AZURE_CONTENT_SAFETY_KEY")
    auth_scope: str = "https://cognitiveservices.azure.com/.default"
    api_version: str = _env("AZURE_CONTENT_SAFETY_API_VERSION", "2024-09-01")
    block_severity: int = int(_env("AZURE_CONTENT_SAFETY_BLOCK_SEVERITY", "4"))  # 0, 2, 4, 6

    @property
    def configured(self) -> bool:
        return bool(self.endpoint)


@dataclass(frozen=True)
class Settings:
    mcp_gateway_url: str = _env("MCP_GATEWAY_URL", "http://localhost:8200")
    mcp_timeout_s: float = float(_env("MCP_TIMEOUT_S", "8"))
    backend_admin_url: str = _env("BACKEND_URL", "http://localhost:8100")
    voice: VoiceSettings = field(default_factory=VoiceSettings)
    content_safety: ContentSafetySettings = field(default_factory=ContentSafetySettings)

    # Formats
    card_prefix: str = _env("CARD_PREFIX", "60012300")
    card_length: int = int(_env("CARD_LENGTH", "19"))
    order_number_length: int = int(_env("ORDER_NUMBER_LENGTH", "10"))

    # Guardrails
    guardrail_timeout_s: float = float(_env("GUARDRAIL_TIMEOUT_S", "2.0"))
    guardrail_fail_mode: str = _env("GUARDRAIL_FAIL_MODE", "open")  # open | closed

    # Conversation behaviour
    idv_max_retries: int = int(_env("IDV_MAX_RETRIES", "1"))
    progress_update_ms: int = int(_env("PROGRESS_UPDATE_MS", "1500"))
    # Pause after an acknowledgement / progress line has finished playing, before the answer starts.
    post_ack_delay_ms: int = int(_env("POST_ACK_DELAY_MS", "1000"))
    # Demo: close the session once the transfer message has played. Real telephony (false): stay connected,
    # the warm transfer takes the call over.
    demo_cut_call_on_transfer: bool = _bool("DEMO_CUT_CALL_ON_TRANSFER", True)
    # Caller silent after a question: check in after this long; close as abandoned after the second check-in.
    silence_reprompt_s: float = float(_env("SILENCE_REPROMPT_S", "15"))
    silence_max_reprompts: int = int(_env("SILENCE_MAX_REPROMPTS", "2"))
    channel: str = _env("CONTACT_CHANNEL", "voice")
    log_level: str = _env("LOG_LEVEL", "INFO")


settings = Settings()
