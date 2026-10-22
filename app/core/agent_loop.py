"""Agent-loop guard: stops a coding agent from falling asleep mid-task.

Gemini Web is trained as a chat assistant. Inside a tool loop (Codex, Cline, OpenCode, OpenHands...) that
shows up as the "conversational promise" stall: the model answers ``I'll inspect the files now and let you
know`` instead of emitting a tool_call. The harness only sees text, concludes the assistant is done and goes
idle -- the user has to nudge it by hand, every single round.

Two defences, both inert when the request declares no tools:

1. ``build_tool_prompt`` appends the reminder to the *end* of the flattened prompt (a prefix-only
   instruction is what the model forgets first in long turns). See ``app/utils/tools.py``.
2. ``guard_tool_loop`` catches the residual stall -- a reply that parses as plain text yet reads as a
   promise of future work -- and asks the model once more with the corrective nudge appended. One retry,
   never a loop; the first reply is kept if the retry does not produce a tool_call either.

The guard is deliberately conservative: it never fires on a reply that contains a code fence, on a reply
that already parses as a tool call, or on text longer than ``_MAX_PROMISE_CHARS`` (long answers are work
product, not promises). Every correction is logged and counted so ``/admin/status`` can show how often the
loop needed rescuing.
"""
from __future__ import annotations

import logging
import re
import threading

logger = logging.getLogger(__name__)

# Phrases that mean "I am about to do it" rather than "I did it". Multi-language because the end user here
# talks to the model in Slovak/English and the model mirrors the conversation language.
_PROMISE_PATTERNS = (
    # English
    r"\bi(?:'ll| will) (?:now |immediately |first )?(?:inspect|look|check|read|start|begin|proceed|review|analyse|analyze|implement|create|build|update|write|run|continue|handle)",
    r"\bi(?:'m| am) (?:now )?(?:going to|about to|starting to)",
    r"\blet me (?:start|begin|first|now|check|look|inspect|read|walk)",
    r"\bworking on (?:it|this|that) now",
    r"\bi(?:'m| am) working on\b",
    r"\b(?:will|i(?:'ll| will)) (?:report|let you know|keep you posted|update you)\b",
    r"\b(?:starting|beginning) (?:the )?(?:implementation|work|task|process)\b",
    r"\bnext steps? (?:are|will be|:)\b",
    r"\bgive me a (?:moment|minute|second)\b",
    r"\bstanding by\b",
    # Slovak / Czech (the model mirrors the language of the conversation)
    r"\bpracujem na (?:tom|ňom|tomto)\b",
    r"\bidem (?:na to|sa do toho|to spraviť|to urobiť|pokračovať)\b",
    r"\bzačínam\b|\bzacnem\b|\bzačnem\b|\bspustil som realizáciu\b|\bspustam realizaciu\b",
    r"\bdám (?:ti|vám) vedieť\b|\bdam (?:ti|vam) vediet\b",
    r"\bbudem (?:ťa|vás|ta|vas) informovať\b|\bbudem informovat\b",
    r"\bpripravujem\b|\bchystám sa\b|\bchystam sa\b",
    r"\bpokračujem\b|\bpokracujem\b",
)

_PROMISE_RE = re.compile("|".join(_PROMISE_PATTERNS), re.IGNORECASE)
# A long reply is work product (an answer, a plan, code), not a stall. Only short promise-shaped replies
# are worth one corrective round trip.
_MAX_PROMISE_CHARS = 1200

NUDGE = (
    "\n\n[MANDATORY CORRECTION] Your previous message did not contain a tool_call — it only described what "
    "you were about to do. There is no background worker and no progress to report: only a tool_call does "
    "the work. Do NOT restate intent, do NOT announce a plan, do NOT promise to report back. Emit the "
    "tool_call now, in this message, as the single JSON object defined above."
)

_SIMULATED_EXEC_PATTERNS = (
    r"import\s+subprocess\b",
    r"from\s+subprocess\s+import",
    r"\bsubprocess\.(?:run|Popen|check_output|call)\b",
    r"\bos\.(?:system|popen|listdir|walk)\b",
    r"\b(?:Get-ChildItem|Select-Object|Where-Object)\b",
    r"\bgit\s+(?:log|status|diff|checkout|branch)\b",
    r"\bpython(?:\.exe)?\s+scripts[\\/]",
    r"\bopen\(.+?,\s*[\"']r[\"']\)",
)
_SIMULATED_EXEC_RE = re.compile("|".join(_SIMULATED_EXEC_PATTERNS), re.IGNORECASE)

SIMULATED_EXEC_NUDGE = (
    "\n\n[MANDATORY CORRECTION] Your previous message contained a code block simulating execution "
    "instead of calling an actual tool. You do NOT have an internal shell or Python sandbox: code in markdown "
    "is never executed. To run commands or inspect files, you MUST emit the JSON tool_calls array immediately "
    "as defined above, with no conversational prose before or after it."
)

_REFUSAL_PATTERNS = (
    r"\bi do not have (?:access|direct access) to (?:an external |the )?local\b",
    r"\bi cannot (?:process or )?read files directly from (?:your |the )?local\b",
    r"\bi do not have access to (?:a |the )?local (?:command shell|terminal|filesystem|sandbox)\b",
    r"\bi cannot (?:access or analyze|directly access or manipulate) (?:the )?local\b",
    r"\btools like `?read_file`?.*?in this environment\b",
    r"\bpermission to turn on the (?:workspace|google drive|google docs) app\b",
    r"\bif you would like to paste the contents\b",
    r"\bplease paste the contents\b",
    r"\bas an ai(?: language model)?,? i do not have access\b",
)
_REFUSAL_RE = re.compile("|".join(_REFUSAL_PATTERNS), re.IGNORECASE)

REFUSAL_NUDGE = (
    "\n\n[MANDATORY CORRECTION] You refused execution claiming you lack filesystem or tool access. "
    "This is FALSE: your tools (read_file, write_file, python_eval, exec) are active and connected "
    "directly to the local workspace. Do NOT refuse. Do NOT output conversational prose. "
    "Call the appropriate tool now in JSON format."
)

_lock = threading.Lock()
_stalls_seen = 0        # promise-shaped replies detected
_stalls_corrected = 0   # of those, how many the retry turned into a tool_call


def stats() -> dict:
    with _lock:
        return {"detected": _stalls_seen, "corrected": _stalls_corrected}


def reset_stats() -> None:
    global _stalls_seen, _stalls_corrected
    with _lock:
        _stalls_seen = 0
        _stalls_corrected = 0


def looks_like_stall(text: str) -> bool:
    """True when the reply reads as a promise of future work, i.e. an agent-loop stall.

    Conservative by construction: code fences, a tool-call payload, long answers and plain questions are
    all ignored (a request for clarification is a legitimate turn end).
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped or len(stripped) > _MAX_PROMISE_CHARS:
        return False
    if "```" in stripped:
        return False
    if '"tool_calls"' in stripped or '"tool_use"' in stripped:
        return False
    return bool(_PROMISE_RE.search(stripped))


def looks_like_simulated_execution(text: str) -> bool:
    """True when the reply contains a code block simulating execution instead of calling tools."""
    if not isinstance(text, str):
        return False
    if '"tool_calls"' in text or '"tool_use"' in text or ('"name"' in text and '"arguments"' in text):
        return False
    blocks = re.findall(r"```(?:[a-zA-Z0-9_-]+)?\s*([\s\S]*?)```", text)
    for b in blocks:
        if _SIMULATED_EXEC_RE.search(b):
            return True
    return False


def looks_like_refusal(text: str) -> bool:
    """True when the reply reads as a soft refusal claiming lack of filesystem or tool access."""
    if not isinstance(text, str):
        return False
    if '"tool_calls"' in text or '"tool_use"' in text or ('"name"' in text and '"arguments"' in text):
        return False
    stripped = text.strip()
    return bool(_REFUSAL_RE.search(stripped))


async def guard_tool_loop(text: str, regenerate, *, enabled: bool = True, budget=None) -> tuple[str, bool]:
    """Return ``(text, corrected)``, asking the model once more when it stalled on a promise.

    ``regenerate(nudge)`` must re-run the upstream call with ``nudge`` appended to the prompt. Any failure
    (exception, empty reply, still no tool call) keeps the original text: this guard may never turn a
    graceful answer into an error. Disabled, or nothing to correct → the input is returned untouched.
    ``budget`` is shared with the malformed-JSON parser so the two safeguards allow only one regeneration
    per logical turn.
    """
    global _stalls_seen, _stalls_corrected
    stall = looks_like_stall(text)
    simulated = looks_like_simulated_execution(text)
    refusal = looks_like_refusal(text)
    if not enabled or regenerate is None or (not stall and not simulated and not refusal):
        return text, False

    if budget is None:
        from app.core.regeneration_budget import RegenerationBudget
        budget = RegenerationBudget()
    if not budget.try_consume("guard"):
        logger.warning("Agent-loop correction skipped: the per-turn regeneration budget is exhausted")
        return text, False

    with _lock:
        _stalls_seen += 1
    if simulated:
        logger.warning(f"Agent-loop simulated execution detected (code block, no tool_call): {text.strip()[:120]!r} "
                       f"— regenerating with simulated-execution correction")
        nudge = SIMULATED_EXEC_NUDGE
    elif refusal:
        logger.warning(f"Agent-loop soft refusal detected (denial of tool access): {text.strip()[:120]!r} "
                       f"— regenerating with refusal correction")
        nudge = REFUSAL_NUDGE
    else:
        logger.warning(f"Agent-loop stall detected (a promise, no tool_call): {text.strip()[:120]!r} "
                       f"— regenerating once with the tool-loop correction")
        nudge = NUDGE

    try:
        retry_text = await regenerate(nudge)
    except Exception as e:  # noqa: BLE001 - the guard must never break the reply path
        logger.warning(f"Tool-loop correction failed, keeping the first reply: {type(e).__name__}: {e}")
        return text, False

    if not isinstance(retry_text, str) or not retry_text.strip():
        return text, False
    if not _has_tool_payload(retry_text):
        logger.warning("Tool-loop correction did not produce a tool_call either; keeping the first reply")
        return text, False

    with _lock:
        _stalls_corrected += 1
    logger.info("Tool-loop correction produced a tool_call; using it")
    return retry_text, True


def _has_tool_payload(text: str) -> bool:
    """Cheap structural check that the corrected reply carries a tool call (parsing happens later)."""
    stripped = text.strip()
    return ('"tool_calls"' in stripped or '"tool_use"' in stripped
            or ('"name"' in stripped and '"arguments"' in stripped))
