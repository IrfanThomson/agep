"""Pluggable LLM backends for AGEP.

Three backends are selectable via the `AGEP_LLM` env var:

* ``claude-code`` (default) — no API key. Uses the local Claude Code
  authentication via ``claude-agent-sdk``. This ships as a custom
  :class:`BaseLlm` subclass (:class:`ClaudeCodeLlm`) because ADK is
  Gemini-first but exposes a clean adapter interface for other backends.
* ``anthropic`` — routes through ADK's ``LiteLlm`` wrapper. Requires
  ``ANTHROPIC_API_KEY`` *and* the LiteLLM extras
  (``pip install "google-adk[extensions]"``). Imported lazily so the default
  path stays lean.
* ``gemini`` — passes the model id string directly to ``LlmAgent``. Requires
  ``GOOGLE_API_KEY``.

The adapter for Claude Code does prompt-level tool orchestration (serializes
tool schemas into the system prompt and asks Claude to respond as JSON). This
is less robust than the provider-native tool-use path used by the other two
backends, but it's what buys the "no API key needed for local dev" story.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, AsyncGenerator, Union

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types as genai_types

logger = logging.getLogger(__name__)

DEFAULT_CLAUDE_CODE_MODEL = "sonnet"
DEFAULT_ANTHROPIC_MODEL = "anthropic/claude-sonnet-4-5"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

_SUPPORTED_BACKENDS = {"claude-code", "anthropic", "gemini"}

# Claude Code's built-in tools. We disable ALL of them when routing through
# the SDK — the adapter does prompt-level tool-use instead, and letting the
# subprocess attempt its own tool calls (especially Task, which spawns
# subagents) destabilizes the subprocess.
_CLAUDE_CODE_DISABLED_TOOLS: list[str] = [
    "Task",
    "AskUserQuestion",
    "Bash",
    "Edit",
    "Write",
    "Read",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "TodoWrite",
    "NotebookEdit",
    "EnterPlanMode",
    "ExitPlanMode",
    "EnterWorktree",
    "ExitWorktree",
    "ScheduleWakeup",
    "Skill",
    "ToolSearch",
]


def get_model(temperature: float) -> Union[str, BaseLlm]:
    """Return the configured model for an agent.

    Reads ``AGEP_LLM`` (backend selector) and ``AGEP_MODEL`` (optional
    override). Returns a value suitable for the ``model=`` kwarg on
    :class:`google.adk.agents.LlmAgent`:

    * ``"claude-code"`` → a :class:`ClaudeCodeLlm` instance
    * ``"anthropic"``   → a :class:`LiteLlm` instance (lazy import)
    * ``"gemini"``      → a plain model-id string
    """
    backend = os.getenv("AGEP_LLM", "claude-code").lower()
    model_override = os.getenv("AGEP_MODEL")

    if backend == "claude-code":
        return ClaudeCodeLlm(
            model=model_override or DEFAULT_CLAUDE_CODE_MODEL,
            temperature=temperature,
        )
    if backend == "anthropic":
        try:
            from google.adk.models.lite_llm import LiteLlm
        except ImportError as e:
            raise RuntimeError(
                "AGEP_LLM=anthropic requires LiteLLM support. Install with: "
                'pip install "google-adk[extensions]"'
            ) from e
        return LiteLlm(model=model_override or DEFAULT_ANTHROPIC_MODEL)
    if backend == "gemini":
        return model_override or DEFAULT_GEMINI_MODEL

    raise ValueError(
        f"Unknown AGEP_LLM backend: {backend!r}. Expected one of {_SUPPORTED_BACKENDS}."
    )


# ---------------------------------------------------------------------------
# Claude Code backend
# ---------------------------------------------------------------------------


_TEMPERATURE_STYLE_HINTS = {
    0.0: "Be strictly deterministic. Prefer the same answer every time.",
    0.1: "Be highly precise and literal.",
    0.7: "Be creative; explore a reasonable breadth of options.",
}


def _temperature_hint(temperature: float) -> str:
    # Snap to the nearest known bucket; AGEP only uses 0.0, 0.1, 0.7.
    nearest = min(_TEMPERATURE_STYLE_HINTS.keys(), key=lambda k: abs(k - temperature))
    return _TEMPERATURE_STYLE_HINTS[nearest]


class ClaudeCodeLlm(BaseLlm):
    """:class:`BaseLlm` adapter that calls Claude Code via ``claude-agent-sdk``.

    Does not require an API key — uses the local Claude Code authentication.

    Tool-use is handled at the prompt level: when the underlying agent has
    tools registered, we append a JSON protocol directive to the system prompt
    and parse the JSON out of Claude's response. Temperature is honoured as a
    soft style hint (the Claude Code CLI does not expose a temperature flag).
    """

    temperature: float = 0.0

    @classmethod
    def supported_models(cls) -> list[str]:
        # Not registered in LlmRegistry — users construct it explicitly.
        return []

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self._maybe_append_user_content(llm_request)

        system_prompt = self._build_system_prompt(llm_request)
        conversation = self._serialize_conversation(llm_request)

        raw_text = await self._invoke_claude(system_prompt, conversation)
        content = self._parse_response(raw_text, llm_request)

        yield LlmResponse(content=content, partial=False, turn_complete=True)

    # ---------- prompt construction ----------

    def _build_system_prompt(self, llm_request: LlmRequest) -> str:
        pieces: list[str] = [_temperature_hint(self.temperature)]

        sys_instr = self._extract_system_instruction(llm_request)
        if sys_instr:
            pieces.append(sys_instr)

        tool_schemas = self._collect_tool_schemas(llm_request)
        if tool_schemas:
            pieces.append(self._tool_use_directive(tool_schemas))

        return "\n\n".join(pieces)

    @staticmethod
    def _extract_system_instruction(llm_request: LlmRequest) -> str:
        si = getattr(llm_request.config, "system_instruction", None)
        if si is None:
            return ""
        if isinstance(si, str):
            return si
        # Content object
        parts = getattr(si, "parts", None) or []
        return "\n".join(p.text for p in parts if getattr(p, "text", None))

    @staticmethod
    def _collect_tool_schemas(llm_request: LlmRequest) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        tools_dict = getattr(llm_request, "tools_dict", None) or {}
        for tool in tools_dict.values():
            get_decl = getattr(tool, "_get_declaration", None)
            if not callable(get_decl):
                continue
            decl = get_decl()
            if decl is None:
                continue
            try:
                schemas.append(json.loads(decl.model_dump_json(exclude_none=True)))
            except Exception:
                schemas.append({"name": getattr(decl, "name", "unknown")})
        return schemas

    @staticmethod
    def _tool_use_directive(tool_schemas: list[dict[str, Any]]) -> str:
        # Carefully worded: when Claude Code sees the word "tool" it attempts
        # to do native function-calling, which crashes the subprocess. We
        # describe the protocol as structured JSON output naming external
        # functions, never as "tools".
        schema_json = json.dumps(tool_schemas, indent=2)
        return (
            "You are in a multi-agent pipeline. Your only job is to emit ONE "
            "JSON object describing what should happen next. You do NOT execute "
            "anything yourself — another component downstream executes the "
            "named function and feeds the result back.\n\n"
            "Respond with EXACTLY one JSON object matching one of these two "
            "shapes, with no prose around it and no markdown fences:\n\n"
            "Shape A — request a function to be invoked:\n"
            '   {"action": "tool_call", "tool_name": "<function name>", "arguments": { ... }}\n\n'
            "Shape B — emit a final text answer:\n"
            '   {"action": "text", "content": "<your response>"}\n\n'
            "Available function names and their argument schemas "
            "(these are references, not tools you can invoke directly):\n"
            f"{schema_json}"
        )

    @staticmethod
    def _serialize_conversation(llm_request: LlmRequest) -> str:
        lines: list[str] = []
        for content in llm_request.contents or []:
            role = content.role or "user"
            for part in content.parts or []:
                if part.text:
                    lines.append(f"[{role}] {part.text}")
                elif part.function_call:
                    fc = part.function_call
                    lines.append(
                        f"[{role}] <tool_call name={fc.name} "
                        f"args={json.dumps(fc.args or {})}>"
                    )
                elif part.function_response:
                    fr = part.function_response
                    lines.append(
                        f"[{role}] <tool_result name={fr.name} "
                        f"response={json.dumps(fr.response or {})}>"
                    )
        return "\n".join(lines) if lines else "[user] (continue)"

    # ---------- model invocation ----------

    async def _invoke_claude(self, system_prompt: str, user_prompt: str) -> str:
        import sys
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            TextBlock,
            query,
        )

        # Claude Code's subprocess deadlocks if we pass a Python stderr=callback
        # (observed: the subprocess blocks on a full stderr pipe the callback
        # can't drain fast enough). Inherit stderr when AGEP_DEBUG is set so the
        # user can see what the CLI is doing; otherwise discard it quietly.
        debug_stderr = sys.stderr if os.getenv("AGEP_DEBUG") else None

        # We can't just use allowed_tools=[] or permission_mode="plan" — both
        # have been observed to cause the subprocess to crash. Explicitly
        # disallowing every built-in tool keeps it on a pure Q&A path, which is
        # exactly what AGEP needs from it.
        options = ClaudeAgentOptions(
            model=self.model,
            system_prompt=system_prompt,
            max_turns=1,
            disallowed_tools=_CLAUDE_CODE_DISABLED_TOOLS,
            debug_stderr=debug_stderr,
        )

        chunks: list[str] = []
        try:
            async for msg in query(prompt=user_prompt, options=options):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            chunks.append(block.text)
        except Exception:
            logger.error(
                "Claude Code call failed. system_prompt head: %r | user head: %r",
                system_prompt[:400],
                user_prompt[:400],
            )
            raise
        return "".join(chunks).strip()

    # ---------- response parsing ----------

    def _parse_response(
        self, raw_text: str, llm_request: LlmRequest
    ) -> genai_types.Content:
        has_tools = bool(getattr(llm_request, "tools_dict", None))
        if not has_tools:
            return _text_content(raw_text or "")

        payload = _try_extract_json(raw_text)
        if payload is None:
            # Not JSON at all — common when the agent has finished and is
            # emitting free-form text. Pass the raw text through.
            return _text_content(raw_text)

        action = payload.get("action")
        if action == "tool_call":
            name = payload.get("tool_name") or payload.get("name") or ""
            args = payload.get("arguments") or payload.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            return _function_call_content(name, args)
        if action == "text":
            return _text_content(payload.get("content", ""))

        # Non-action JSON — likely the agent's final structured output (a
        # Critique, SafetyAudit, or MenuPlan). Pass the raw JSON string through
        # as text so the output_key captures it verbatim for the next agent.
        return _text_content(raw_text)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _try_extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction from an LLM text response.

    Handles markdown code fences and stray prose around the JSON object.
    """
    if not text:
        return None
    stripped = _FENCE_RE.sub("", text).strip()
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    # Fall back to locating the first balanced {...} block.
    start = stripped.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(stripped)):
            ch = stripped[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = stripped[start : i + 1]
                    try:
                        parsed = json.loads(candidate)
                        return parsed if isinstance(parsed, dict) else None
                    except json.JSONDecodeError:
                        break
        start = stripped.find("{", start + 1)
    return None


def _text_content(text: str) -> genai_types.Content:
    return genai_types.Content(
        role="model",
        parts=[genai_types.Part(text=text or "")],
    )


def _function_call_content(name: str, args: dict[str, Any]) -> genai_types.Content:
    return genai_types.Content(
        role="model",
        parts=[
            genai_types.Part(
                function_call=genai_types.FunctionCall(name=name, args=args)
            )
        ],
    )
