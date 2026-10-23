"""Boss orchestration and worker handoffs."""

from __future__ import annotations

import asyncio
from typing import Any

from .prompt_loader import get_prompt
from .models import BuyerSafetyVerdict, FlipSafeAppraisalPackage, ListingAndSafetyResult
from .tools import (
    SUPPORTED,
    WorkflowStop,
    calculate_price_tiers_math,
    live_web_comp_search,
    optimize_and_inspect_image,
    portkey_json,
    pricing_tool,
    sample_tool,
    verify_listing_compliance,
)
from .telemetry import telemetry
from .vision_agent import inspect_with_pydantic_ai


def _normalized_category(value: str, fallback: str = "") -> str:
    text = (value or "").strip().lower()
    if "headset" in text or "headphone" in text:
        return "Headphones"
    if "ssd" in text or "solid state" in text or "nvme" in text:
        return "SSD"
    if "laptop" in text or "notebook" in text:
        return "Laptop"
    if "phone" in text or "smartphone" in text:
        return "Smartphone"
    if "monitor" in text or "display" in text:
        return "IPS monitor"
    if "keyboard" in text:
        return "Mechanical keyboard"
    return fallback if fallback in SUPPORTED else ""


def _pricing_condition(inspection: dict[str, Any]) -> str:
    estimate = inspection.get("condition_estimate", "")
    if estimate in {"Like new", "Good", "Fair", "For parts / not working"}:
        return estimate
    raise WorkflowStop("Vision could not determine a usable cosmetic condition; pricing is paused.", ["vision-assessed condition"])


async def photo_inspection_worker(*, image: str, category: str, notes: str, condition: str, run_id: str | None = None, optimized: bool = True) -> dict[str, Any]:
    if category not in SUPPORTED and category not in {"", "auto", "unknown", "Unknown"}:
        raise WorkflowStop(f"Unsupported item category: {category}.", ["supported electronics category"])
    image_info = optimize_and_inspect_image(image)
    if run_id:
        telemetry.event(run_id, "tool", "photo_inspection", "Optimized uploaded image for vision", tool="optimize_and_inspect_image", arguments={"original_size": image_info["original_size"], "optimized_size": image_info["optimized_size"]}, result_summary=f"{image_info['bytes']} bytes sent to vision")
    result = (await inspect_with_pydantic_ai(
        image_bytes=image_info["raw_bytes"],
        mime_type=image_info["mime_type"],
        category=category,
        notes=notes,
        condition=condition,
        run_id=run_id,
        optimized=optimized,
    )).model_dump()
    result["seller_notes"] = notes or ""
    result["image_evidence"] = {"mime_type": image_info["mime_type"], "bytes": image_info["bytes"], "original_size": image_info["original_size"], "optimized_size": image_info["optimized_size"]}
    result["agent"] = "photo_inspection"
    result["category"] = _normalized_category(str(result.get("category", "")), category)
    if not result["category"]:
        result["supported"] = False
        result["missing_evidence"] = list(result.get("missing_evidence", [])) + ["supported electronics category"]
    return result


def pricing_worker(*, inspection: dict[str, Any], category: str, condition: str, notes: str, run_id: str | None = None, optimized: bool = True) -> dict[str, Any]:
    if not inspection.get("supported", True):
        raise WorkflowStop("Photo inspection marked this item as unsupported.", inspection.get("missing_evidence", []))
    category = _normalized_category(str(inspection.get("category", "")), category)
    if category not in SUPPORTED:
        raise WorkflowStop("Vision could not identify a supported electronics category.", ["supported electronics category"])
    verified_specs = " ".join(str(item) for item in inspection.get("verified_specs", []))
    item_hint = str(inspection.get("item_name", ""))
    notes_hint = str(inspection.get("seller_notes", ""))
    if run_id:
        web = live_web_comp_search(category, item_hint)
        telemetry.event(run_id, "tool", "pricing", "Checked optional market context", tool="live_web_comp_search", result_summary=web["message"])
    condition = _pricing_condition(inspection)
    price = calculate_price_tiers_math(category, condition, f"{item_hint} {notes_hint}", verified_specs)
    if run_id:
        telemetry.event(run_id, "tool", "pricing", "Selected relevant sold comparisons", tool="comparison_tool", evidence_count=price["evidence_count"])
        telemetry.event(run_id, "result", "pricing", "Calculated four prices in Python", tool="pricing_tool", prices={key: price[key] for key in ("quick_sale", "fair_market", "listing_price", "minimum_acceptable_offer")})
    price["agent"] = "pricing"
    price["input_handoff"] = {"item_name": item_hint, "category": category, "condition": condition, "verified_specs": inspection.get("verified_specs", [])}
    return price


async def listing_safety_worker(*, inspection: dict[str, Any], pricing: dict[str, Any], notes: str, buyer_message: str, run_id: str | None = None, optimized: bool = True) -> dict[str, Any]:
    messages = [
        {"role": "system", "content": telemetry.instruction("listing_and_safety", get_prompt("listing_scam_prompt"), optimized)},
        {"role": "user", "content": f"INSPECTION HANDOFF:\n{inspection}\n\nCODE PRICING HANDOFF:\n{pricing}\n\nSELLER NOTES:\n{notes or 'none'}\n\nBUYER MESSAGE:\n{buyer_message or 'none supplied'}"},
    ]
    result = await asyncio.to_thread(portkey_json, messages, run_id=run_id, agent="listing_and_buyer_safety", optimized=optimized)
    safety = dict(result.get("safety") or {})
    risk_map = {"low": "SAFE", "medium": "SUSPICIOUS", "high": "SUSPICIOUS", "blocked": "BLOCKED_ATTACK", "not_checked": "not_checked"}
    safety["risk"] = risk_map.get(str(safety.get("risk", "not_checked")).lower(), str(safety.get("risk", "not_checked")).upper())
    safety.setdefault("threats", safety.get("signals", []))
    safety_model = BuyerSafetyVerdict.model_validate(safety)
    result["safety"] = safety_model.model_dump()
    result = ListingAndSafetyResult.model_validate(result).model_dump()
    result["agent"] = "listing_and_buyer_safety"
    result["input_handoff"] = {"price": pricing, "condition": pricing.get("condition_used"), "buyer_message_supplied": bool(buyer_message)}
    return result


class BossAgent:
    """Delegates the workflow and records worker/tool handoffs for the UI."""

    system_prompt = get_prompt("boss_prompt")

    async def appraise(self, *, image: str, category: str, condition: str, notes: str, buyer_message: str = "", sample_id: str = "", run_id: str | None = None, optimized: bool = True) -> dict[str, Any]:
        trace: list[dict[str, Any]] = []
        sample = sample_tool(sample_id) if sample_id else None
        if sample:
            notes = sample["notes"]
        try:
            if run_id:
                telemetry.event(run_id, "start", "boss", "Boss delegated the appraisal workflow", mode="optimized" if optimized else "unoptimized")
                if not telemetry.limit_status(run_id)["allowed"]:
                    limit = telemetry.limit_status(run_id)
                    raise WorkflowStop(limit["message"], [limit["code"]])
            trace.append({"agent": "boss", "action": "delegate", "next": "photo_inspection"})
            if run_id:
                telemetry.event(run_id, "start", "photo_inspection", "Photo Inspector started", tool="pydantic_ai")
            photo = await photo_inspection_worker(image=image, category=category, notes=notes, condition=condition, run_id=run_id, optimized=optimized)
            if run_id and not telemetry.limit_status(run_id)["allowed"]:
                limit = telemetry.limit_status(run_id)
                raise WorkflowStop(limit["message"], [limit["code"]])
            trace.append({"agent": "photo_inspection", "tool": "pydantic_ai_portkey_vision", "handoff": ["item_name", "supported", "verified_specs", "missing_evidence"]})
            if not photo.get("supported", True):
                raise WorkflowStop("The photo worker could not verify a supported electronics category.", photo.get("missing_evidence", []))
            trace.append({"agent": "boss", "action": "delegate", "next": "pricing"})
            telemetry.instruction("pricing", get_prompt("pricing_prompt"), optimized)
            category = _normalized_category(str(photo.get("category", "")), category)
            pricing = pricing_worker(inspection=photo, category=category, condition=condition, notes=notes, run_id=run_id, optimized=optimized)
            trace.append({"agent": "pricing", "tools": ["comparison_tool", "pricing_tool"], "handoff": ["quick_sale", "fair_market", "listing_price", "minimum_acceptable_offer"]})
            trace.append({"agent": "boss", "action": "delegate", "next": "listing_and_buyer_safety"})
            if run_id:
                telemetry.event(run_id, "start", "listing_and_buyer_safety", "Listing and safety worker started", tool="portkey")
            copy = await listing_safety_worker(inspection=photo, pricing=pricing, notes=notes, buyer_message=buyer_message, run_id=run_id, optimized=optimized)
            compliance = verify_listing_compliance(pricing=pricing, inspection=photo, listing=copy)
            if run_id:
                telemetry.event(run_id, "tool", "listing_and_buyer_safety", "Checked listing claims and price compliance", tool="verify_listing_compliance", result_summary="compliant" if compliance["compliant"] else "blocked")
            if not compliance["compliant"]:
                raise WorkflowStop("Listing compliance failed; unsupported claims or invalid prices need review.", compliance["issues"])
            if run_id and not telemetry.limit_status(run_id)["allowed"]:
                limit = telemetry.limit_status(run_id)
                raise WorkflowStop(limit["message"], [limit["code"]])
            trace.append({"agent": "listing_and_buyer_safety", "tool": "portkey_json", "handoff": ["copy_ready_listing", "safety"]})
            package = FlipSafeAppraisalPackage(status="ready", run_id=run_id or "", category=category, inspection=photo, pricing=pricing, listing_and_safety=copy, trace=trace)
            return package.model_dump()
        except WorkflowStop as stop:
            if run_id:
                telemetry.event(run_id, "error", "boss", stop.reason, missing_evidence=stop.missing)
            trace.append({"agent": "boss", "action": "stop", "reason": stop.reason})
            return FlipSafeAppraisalPackage(status="stopped", run_id=run_id or "", category=category, reason=stop.reason, missing_evidence=stop.missing, trace=trace).model_dump()

    async def buyer_check(self, *, message: str, listing_context: str = "", run_id: str | None = None, optimized: bool = True) -> dict[str, Any]:
        if run_id and not telemetry.limit_status(run_id)["allowed"]:
            limit = telemetry.limit_status(run_id)
            raise WorkflowStop(limit["message"], [limit["code"]])
        messages = [
            {"role": "system", "content": telemetry.instruction("listing_and_safety", get_prompt("listing_scam_prompt"), optimized)},
            {"role": "user", "content": f"No appraisal is being requested. Review this buyer message only. Listing context: {listing_context or 'none'}\nBuyer message: {message}"},
        ]
        result = await asyncio.to_thread(portkey_json, messages, run_id=run_id, agent="listing_and_buyer_safety", optimized=optimized)
        safety = dict(result.get("safety") or result)
        risk_map = {"low": "SAFE", "medium": "SUSPICIOUS", "high": "SUSPICIOUS", "blocked": "BLOCKED_ATTACK", "not_checked": "not_checked"}
        safety["risk"] = risk_map.get(str(safety.get("risk", "not_checked")).lower(), str(safety.get("risk", "not_checked")).upper())
        safety.setdefault("threats", safety.get("signals", []))
        result = BuyerSafetyVerdict.model_validate(safety).model_dump()
        if run_id and not telemetry.limit_status(run_id)["allowed"]:
            limit = telemetry.limit_status(run_id)
            raise WorkflowStop(limit["message"], [limit["code"]])
        return {"status": "ready", "agent": "listing_and_buyer_safety", "safety": result, "trace": [{"agent": "boss", "action": "delegate", "next": "listing_and_buyer_safety"}, {"agent": "listing_and_buyer_safety", "tool": "portkey_json"}]}
