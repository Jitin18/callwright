"""The coordinator's configuration: welcome line, prompt rules, tools and turn detection (no Azure)."""
from services.agent_runtime.config import PROMPTS_DIR, settings
from services.agent_runtime.tools import GREETING_INSTRUCTIONS, WELCOME_LINE, instructions, tool_definitions
from services.agent_runtime.cascade import session_config, turn_detection, turn_detection_update


def test_welcome_line_is_exact_and_first():
    assert WELCOME_LINE == "Welcome to FreshCart AI Assisted Customer Service."
    assert f'Begin with exactly these words: "{WELCOME_LINE}"' in GREETING_INSTRUCTIONS
    assert WELCOME_LINE in instructions()


def test_prompt_forbids_commanding_and_goodbye_after_transfer():
    p = instructions()
    assert "NEVER tell the customer how to answer" in p
    assert "say yes or no" in p and "digits only" in p                     # named as forbidden examples
    assert "internal guidance" in p and "parse_hint" in p
    assert "NEVER say goodbye" in p and "transferred: true" in p
    assert "say a brief goodbye and call `end_call`" not in p             # the old transfer rule is gone
    assert "let them finish" in p                                         # long input


def test_prompt_and_subagent_notes_have_no_answer_format_instructions_for_customers():
    # Formats may only appear as internal hints; the slot descriptions the model reads are plain meaning.
    from services.agent_runtime.journeys.dsl import Ask
    from services.agent_runtime.journeys.registry import JOURNEYS
    for j in JOURNEYS.values():
        for n in j.nodes:
            for sl in (n.slots if isinstance(n, Ask) else []):
                assert "yyyy" not in sl.description and "digits" not in sl.description.lower(), (j.id, sl.name)


def test_session_config_and_mid_session_turn_detection():
    cfg = session_config(settings.voice, instructions(), tool_definitions())
    assert cfg["session"]["modalities"] == ["text", "audio"]
    assert session_config(settings.voice, "x", [], voice_out=False)["session"]["modalities"] == ["text"]
    assert turn_detection(settings.voice)["silence_duration_ms"] == settings.voice.vad_silence_ms
    upd = turn_detection_update(settings.voice, long_input=True)
    assert upd["type"] == "session.update" and list(upd["session"]) == ["turn_detection"]
    assert upd["session"]["turn_detection"]["silence_duration_ms"] == settings.voice.vad_silence_ms_long_input
    assert upd["session"]["turn_detection"]["type"] == "silero_vad"


def test_transfer_tool_carries_a_reason():
    t = {x["name"]: x for x in tool_definitions()}["transfer_to_colleague"]
    assert t["parameters"]["required"] == ["reason"] and "can't handle" in t["description"]
    assert (PROMPTS_DIR / "voice_agent.md").exists()
