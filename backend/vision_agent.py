"""Typed PydanticAI vision worker using Portkey's OpenAI-compatible endpoint."""

from __future__ import annotations

import os

from pydantic_ai import Agent, BinaryContent
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

from .config import PORTKEY_BASE_URL
from .prompt_loader import get_prompt
from .models import VisionAppraisalResult
from .security import MAX_TOKENS_PER_CALL
from .telemetry import telemetry
from .tools import WorkflowStop


def _agent(*, optimized: bool) -> Agent:
    api_key = os.getenv("PORTKEY_API_KEY")
    if not api_key:
        raise WorkflowStop("Portkey access is not configured. Add PORTKEY_API_KEY to the local .env file.")
    provider = OpenAIProvider(api_key=api_key, base_url=PORTKEY_BASE_URL)
    model = OpenAIResponsesModel("gpt-6-luna", provider=provider)
    # Portkey's gpt-6-luna deployment does not allow function tools while
    # reasoning_effort is enabled. PydanticAI exposes the OpenAI-compatible
    # setting with its provider-specific name.
    return Agent(
        model,
        system_prompt=telemetry.instruction("vision", get_prompt("vision_prompt"), optimized),
        output_type=VisionAppraisalResult,
        model_settings={
            "max_tokens": MAX_TOKENS_PER_CALL,
            "openai_reasoning_effort": "none",
            "openai_prompt_cache_key": "flipsafe-vision-v1",
            "openai_prompt_cache_retention": "24h",
        },
        retries=1,
    )


async def inspect_with_pydantic_ai(*, image_bytes: bytes, mime_type: str, category: str, notes: str, condition: str, run_id: str | None = None, optimized: bool = True) -> VisionAppraisalResult:
    """Run the typed vision agent through Portkey's Responses API."""
    prompt = (
        f"User-selected category: {category}\n"
        f"Seller condition input (unverified hint only): {condition or 'none supplied'}\n"
        f"Seller notes (claims, not verification): {notes or 'none'}"
    )
    try:
        result = await _agent(optimized=optimized).run([prompt, BinaryContent(data=image_bytes, media_type=mime_type)])
    except WorkflowStop:
        raise
    except Exception as error:
        if error.__class__.__name__ == "ModelConnectionError":
            raise WorkflowStop("Portkey could not be reached from the backend process. Check local network or firewall access to api.portkey.ai.") from error
        raise WorkflowStop("PydanticAI vision inspection failed. Check Portkey configuration and try again.") from error
    if run_id:
        # PydanticAI 1.x exposes usage as a property (older releases used a
        # callable); keep telemetry compatible with the installed version.
        usage = result.usage
        raw_cost = getattr(usage, "cost", None) or getattr(usage, "total_cost", None) or (getattr(usage, "details", {}) or {}).get("cost")
        try:
            cost = float(raw_cost) if raw_cost is not None else None
        except (TypeError, ValueError):
            cost = None
        telemetry.usage_record(run_id, "photo_inspection", "pydantic_ai", usage, cost=cost, optimized=optimized)
        telemetry.event(run_id, "result", "photo_inspection", "Typed vision result validated", tool="pydantic_ai", usage={"request_tokens": getattr(usage, "request_tokens", None), "response_tokens": getattr(usage, "response_tokens", None), "total_tokens": getattr(usage, "total_tokens", None)}, cost=cost)
    return result.output
