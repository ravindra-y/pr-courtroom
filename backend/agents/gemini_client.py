import asyncio
import json
import os
import re
from typing import Type, TypeVar
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

load_dotenv()

T = TypeVar("T", bound=BaseModel)

SECRET_PATTERN = re.compile(
    r"(ghp_[A-Za-z0-9_]+|ghs_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|AIza[A-Za-z0-9_-]+|Bearer\s+[A-Za-z0-9._-]+)"
)


class GeminiClientError(RuntimeError):
    """Friendly error raised when Gemini API call or structured JSON parsing fails."""


def redact_secrets(text: str) -> str:
    """Redact any API keys or tokens from error strings."""
    cleaned = SECRET_PATTERN.sub("[REDACTED]", text or "")
    for env_var in ("GEMINI_API_KEY", "GITHUB_TOKEN"):
        val = os.getenv(env_var, "").strip()
        if val and len(val) >= 6:
            cleaned = cleaned.replace(val, "[REDACTED]")
    return cleaned


def _is_rate_limit_error(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code == 429:
        return True
    msg = str(exc).lower()
    return any(
        token in msg
        for token in ("429", "rate limit", "resource_exhausted", "quota exceeded", "too many requests")
    )


def get_gemini_client() -> genai.Client:
    """Create and return a google-genai Client from GEMINI_API_KEY in .env / environment."""
    load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise GeminiClientError(
            "Missing GEMINI_API_KEY. Please set GEMINI_API_KEY in your .env file."
        )
    return genai.Client(api_key=api_key)


def get_fast_model() -> str:
    load_dotenv()
    raw = os.getenv("GEMINI_MODEL_FAST", "").strip()
    if not raw or raw.startswith("<"):
        return "gemini-2.5-flash"
    return raw


def get_judge_model() -> str:
    load_dotenv()
    raw = os.getenv("GEMINI_MODEL_JUDGE", "").strip()
    if not raw or raw.startswith("<"):
        return get_fast_model()
    return raw


async def _call_model_async(
    client: genai.Client,
    model: str,
    system_instruction: str,
    prompt: str,
    schema: Type[T],
    temperature: float,
) -> str:
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        response_mime_type="application/json",
        response_schema=schema,
        temperature=temperature,
    )
    response = await client.aio.models.generate_content(
        model=model,
        contents=prompt,
        config=config,
    )
    return response.text or ""


async def _call_with_rate_limit_retry(
    client: genai.Client,
    model: str,
    system_instruction: str,
    prompt: str,
    schema: Type[T],
    temperature: float,
) -> str:
    """Call _call_model_async and retry ONCE after a short wait on 429 rate-limit errors."""
    wait_seconds = float(os.getenv("GEMINI_RATE_LIMIT_WAIT_SECONDS", "2.0"))
    for rl_attempt in range(2):
        try:
            return await _call_model_async(
                client=client,
                model=model,
                system_instruction=system_instruction,
                prompt=prompt,
                schema=schema,
                temperature=temperature,
            )
        except Exception as exc:
            if _is_rate_limit_error(exc):
                if rl_attempt == 0:
                    await asyncio.sleep(wait_seconds)
                    continue
                raise GeminiClientError(
                    "Gemini API rate limit (429) exceeded after retry. Please wait a moment or switch to Replay mode."
                ) from exc
            safe_detail = redact_secrets(str(exc)).splitlines()[0] if str(exc) else type(exc).__name__
            raise GeminiClientError(
                f"Gemini API request failed for model '{model}': {safe_detail}"
            ) from exc
    raise GeminiClientError(f"Gemini API request failed for model '{model}'.")


async def generate_structured(
    model: str,
    system_instruction: str,
    user_content: str,
    schema: Type[T],
    temperature: float = 0.2,
) -> T:
    """
    Call Gemini asynchronously with a Pydantic response_schema and validate the JSON output.
    - Retries once after a short wait on HTTP 429 rate limit.
    - If JSON parsing fails, retries ONCE with 'Return valid JSON only' appended to the prompt.
    - If it fails again, raises a friendly GeminiClientError.
    """
    client = get_gemini_client()

    for attempt in range(2):
        prompt = (
            user_content
            if attempt == 0
            else f"{user_content}\n\nReturn valid JSON only"
        )
        raw_text = await _call_with_rate_limit_retry(
            client=client,
            model=model,
            system_instruction=system_instruction,
            prompt=prompt,
            schema=schema,
            temperature=temperature,
        )

        try:
            return schema.model_validate_json(raw_text)
        except (ValidationError, json.JSONDecodeError, ValueError):
            continue

    raise GeminiClientError(
        f"Gemini returned invalid JSON after retry for schema {schema.__name__}. Please retry or use Replay mode."
    )
