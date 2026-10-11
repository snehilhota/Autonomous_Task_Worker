"""Small, manually executed Gemini function-calling client with model fallback."""

from dataclasses import dataclass, field
import base64
import os
from pathlib import Path
import time
from typing import Any, Callable, Protocol


class ModelError(RuntimeError):
    """Base class for model configuration and response failures."""


class ModelConfigurationError(ModelError):
    pass


class ModelResponseError(ModelError):
    pass


class ModelUnavailableError(ModelError):
    pass


@dataclass(frozen=True)
class GeminiConfig:
    api_key: str = field(repr=False)
    models: tuple[str, ...]
    timeout_seconds: float = 30


@dataclass(frozen=True)
class FunctionCall:
    name: str
    arguments: dict[str, Any]
    id: str | None = None


@dataclass(frozen=True)
class ModelReply:
    text: str = ""
    function_call: FunctionCall | None = None
    model: str = ""
    model_content: dict[str, Any] | None = None


class GeminiProvider(Protocol):
    def generate(self, model: str, contents: Any, functions: list[dict[str, Any]]) -> Any: ...


def _load_dotenv(path: Path) -> None:
    """Load simple KEY=value entries without replacing explicitly set environment values."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def load_gemini_config(env_file: str | Path = ".env") -> GeminiConfig:
    _load_dotenv(Path(env_file))
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    primary = os.getenv("GEMINI_PRIMARY_MODEL", "").strip()
    fallbacks = [value.strip() for value in os.getenv("GEMINI_FALLBACK_MODELS", "").split(",")
                 if value.strip()]
    if not api_key or api_key in {"your_api_key_here", "replace_me"}:
        raise ModelConfigurationError("Set GEMINI_API_KEY in .env before using Gemini.")
    if not primary or primary in {"your_primary_model", "replace_me"}:
        raise ModelConfigurationError("Set GEMINI_PRIMARY_MODEL in .env to a model you have verified.")
    models = tuple(dict.fromkeys([primary, *fallbacks]))
    return GeminiConfig(api_key=api_key, models=models)


class GoogleGenAIProvider:
    """Thin adapter for the official google-genai SDK; all tool execution stays in Python."""

    def __init__(self, api_key: str, timeout_seconds: float = 30):
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise ModelConfigurationError(
                "Install the project dependencies with `python -m pip install -r requirements.txt`."
            ) from exc
        self._types = types
        self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=int(timeout_seconds * 1000)))

    def generate(self, model: str, contents: Any, functions: list[dict[str, Any]]) -> Any:
        declarations = [
            self._types.FunctionDeclaration(
                name=function["name"], description=function["description"],
                parameters_json_schema=function["parameters"],
            )
            for function in functions
        ]
        config = self._types.GenerateContentConfig(
            tools=[self._types.Tool(function_declarations=declarations)],
            automatic_function_calling=self._types.AutomaticFunctionCallingConfig(disable=True),
        )
        return self._client.models.generate_content(model=model, contents=contents, config=config)


def _status_code(error: Exception) -> int | None:
    for value in (getattr(error, "code", None), getattr(error, "status_code", None),
                  getattr(getattr(error, "response", None), "status_code", None)):
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _retry_after(error: Exception) -> float | None:
    for value in (getattr(error, "retry_after_seconds", None), getattr(error, "retry_after", None)):
        try:
            if value is not None:
                return max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    return None


def _error_category(error: Exception) -> str:
    code = _status_code(error)
    message = str(error).casefold()
    if code in {401, 403} or "unauthorized" in message or "api key" in message:
        return "auth"
    if code == 429 or "quota" in message or "resource exhausted" in message or "rate limit" in message:
        return "quota"
    if code in {400, 404, 406, 415} or any(word in message for word in (
        "unsupported", "not supported", "invalid argument", "function calling is not",
    )):
        return "incompatible"
    if code is not None and code >= 500 or isinstance(error, (TimeoutError, ConnectionError)):
        return "transient"
    return "unknown"


def _parse_response(response: Any, model: str) -> ModelReply:
    calls: list[FunctionCall] = []
    text_parts: list[str] = []
    model_content = None
    blocked = bool(getattr(getattr(response, "prompt_feedback", None), "block_reason", None))
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        reason = str(getattr(candidate, "finish_reason", "")).upper()
        blocked = blocked or any(word in reason for word in ("SAFETY", "BLOCK"))
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            call = getattr(part, "function_call", None)
            if call is not None:
                name = getattr(call, "name", "")
                arguments = getattr(call, "args", None)
                if not isinstance(name, str) or not name or not isinstance(arguments, dict):
                    raise ModelResponseError("Gemini returned malformed function-call arguments.")
                calls.append(FunctionCall(name, arguments, getattr(call, "id", None)))
                model_content = _history_content(content)
            part_text = getattr(part, "text", None)
            if isinstance(part_text, str) and part_text.strip():
                text_parts.append(part_text.strip())
    if len(calls) > 1:
        raise ModelResponseError("Gemini returned multiple function calls; only one is allowed per step.")
    if blocked:
        raise ModelResponseError("Gemini returned a blocked response.")
    text = "\n".join(text_parts)
    if not calls and not text:
        raise ModelResponseError("Gemini returned an empty response.")
    return ModelReply(text=text, function_call=calls[0] if calls else None, model=model,
                      model_content=model_content)


def _history_content(content: Any) -> dict[str, Any]:
    """Keep the exact Gemini response parts needed for the next function turn.

    The official SDK models expose `model_dump`; JSON mode encodes byte-valued
    thought signatures as base64 so task state remains JSON serializable.
    """
    dump = getattr(content, "model_dump", None)
    if callable(dump):
        value = dump(mode="json", by_alias=True, exclude_none=True)
        if isinstance(value, dict):
            value.setdefault("role", "model")
            return value

    parts = []
    for part in getattr(content, "parts", None) or []:
        value = {}
        call = getattr(part, "function_call", None)
        if call is not None:
            function_call = {"name": getattr(call, "name", "")}
            arguments = getattr(call, "args", None)
            if arguments is not None:
                function_call["args"] = arguments
            call_id = getattr(call, "id", None)
            if call_id is not None:
                function_call["id"] = call_id
            value["functionCall"] = function_call
        text = getattr(part, "text", None)
        if text is not None:
            value["text"] = text
        thought = getattr(part, "thought", None)
        if thought is not None:
            value["thought"] = thought
        signature = getattr(part, "thought_signature", None)
        if signature is None:
            signature = getattr(part, "thoughtSignature", None)
        if isinstance(signature, bytes):
            signature = base64.b64encode(signature).decode("ascii")
        if signature is not None:
            value["thoughtSignature"] = signature
        parts.append(value)
    return {"role": "model", "parts": parts}


def _append_nudge(contents: Any) -> list[Any]:
    updated = list(contents) if isinstance(contents, (list, tuple)) else [contents]
    updated.append({"role": "user", "parts": [{
        "text": "Return one valid function call or a concise final answer. Do not return multiple calls."
    }]})
    return updated


class GeminiClient:
    """Calls configured models in order, retries bounded failures, and never runs tools itself."""

    def __init__(
        self,
        config: GeminiConfig,
        provider: GeminiProvider,
        *,
        event_log=None,
        sleep: Callable[[float], None] = time.sleep,
        max_retry_wait_seconds: float = 5,
    ):
        self.config = config
        self.provider = provider
        self.event_log = event_log
        self.sleep = sleep
        self.max_retry_wait_seconds = max_retry_wait_seconds
        self.exhausted_models: set[str] = set()

    def generate(
        self, contents: Any, functions: list[dict[str, Any]], *, run_id: str, step: int,
    ) -> ModelReply:
        last_error: Exception | None = None
        for model in self.config.models:
            if model in self.exhausted_models:
                continue
            for attempt in range(3):
                try:
                    try:
                        reply = self._request(model, contents, functions, run_id, step)
                    except ModelResponseError:
                        reply = self._request(model, _append_nudge(contents), functions, run_id, step)
                    return reply
                except ModelResponseError as exc:
                    raise exc
                except Exception as exc:
                    last_error = exc
                    category = _error_category(exc)
                    if category == "auth":
                        raise ModelConfigurationError("Gemini authentication failed; check GEMINI_API_KEY.") from exc
                    if category == "quota":
                        wait = _retry_after(exc)
                        if attempt == 0 and wait is not None and 0 < wait <= self.max_retry_wait_seconds:
                            self.sleep(wait)
                            continue
                        self.exhausted_models.add(model)
                        break
                    if category == "incompatible":
                        self.exhausted_models.add(model)
                        break
                    if category == "transient" and attempt < 2:
                        self.sleep(0.25 * (2 ** attempt))
                        continue
                    self.exhausted_models.add(model)
                    break
        raise ModelUnavailableError("All configured Gemini models are unavailable or exhausted.") from last_error

    def _request(self, model: str, contents: Any, functions: list[dict[str, Any]],
                 run_id: str, step: int) -> ModelReply:
        started = time.monotonic()
        try:
            response = self.provider.generate(model, contents, functions)
            reply = _parse_response(response, model)
        except ModelResponseError:
            self._record(run_id, step, model, time.monotonic() - started, "invalid_response",
                         error_type="ModelResponseError")
            raise
        except Exception as exc:
            self._record(run_id, step, model, time.monotonic() - started, _error_category(exc),
                         error_type=type(exc).__name__, status_code=_status_code(exc))
            raise
        self._record(run_id, step, model, time.monotonic() - started, "ok")
        return reply

    def _record(self, run_id: str, step: int, model: str, latency: float, outcome: str,
                *, error_type: str | None = None, status_code: int | None = None) -> None:
        if self.event_log is not None:
            self.event_log.append(
                "model_call", run_id=run_id, step=step, model=model,
                latency_seconds=round(latency, 3), outcome=outcome,
                error_type=error_type, status_code=status_code,
            )
