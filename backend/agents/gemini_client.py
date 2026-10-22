import json
import os
from typing import Type, TypeVar
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

load_dotenv()

T = TypeVar("T", bound=BaseModel)


class GeminiClientError(RuntimeError):
    """Friendly error raised when Gemini API call or structured JSON parsing fails."""


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


async def generate_structured(
    model: str,
    system_instruction: str,
    user_content: str,
    schema: Type[T],
    temperature: float = 0.2,
) -> T:
    """
    Call Gemini asynchronously with a Pydantic response_schema and validate the JSON output.
    If parsing fails, retry ONCE with 'Return valid JSON only' appended to the prompt.
    If it fails again, raise a friendly GeminiClientError.
    """
    client = get_gemini_client()
    last_error: Exception | None = None

    for attempt in range(2):
        prompt = (
            user_content
            if attempt == 0
            else f"{user_content}\n\nReturn valid JSON only"
        )
        try:
            raw_text = await _call_model_async(
                client=client,
                model=model,
                system_instruction=system_instruction,
                prompt=prompt,
                schema=schema,
                temperature=temperature,
            )
        except Exception as exc:
            raise GeminiClientError(
                f"Gemini API request failed for model '{model}': {exc}"
            ) from exc

        try:
            return schema.model_validate_json(raw_text)
        except (ValidationError, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            continue

    raise GeminiClientError(
        f"Gemini returned invalid JSON after retry for schema {schema.__name__}: {last_error}"
    )
