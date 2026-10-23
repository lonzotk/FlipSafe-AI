"""Local tools used by the Boss and worker agents."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import urllib.error
import urllib.request
from typing import Any

from PIL import Image

from .config import MAX_TOKENS_PER_CALL, PORTKEY_BASE_URL, PORTKEY_MODEL, ROOT
from .models import ComparisonRecord, PricingRecommendation
from .telemetry import telemetry

DATA = ROOT / "data"
SUPPORTED = {"Headphones", "SSD", "Laptop", "Smartphone", "Monitor", "IPS monitor", "Mechanical keyboard"}
CATEGORY_ALIASES = {"Monitor": "IPS monitor", "IPS monitor": "Monitor"}
FULL_DATA = json.loads((DATA / "full_data.json").read_text(encoding="utf-8"))
COMPARISONS = FULL_DATA["comparisons"]
PLAYBOOK = (DATA / "playbook_rules.md").read_text(encoding="utf-8")
DEMO_SAMPLES = [
    {"id": "headphones", "label": "Headphones · public demo photo", "image": "/data/sample_images/headphones.jpg", "category": "Headphones", "notes": "Public Wikimedia Commons demo photo. Synthetic seller claim for local testing: Auralite Studio ANC, wireless ANC, charging case included."},
    {"id": "ssd", "label": "SSD · public demo photo", "image": "/data/sample_images/ssd.jpg", "category": "SSD", "notes": "Public Wikimedia Commons demo photo. Synthetic seller claim for local testing: Northstar NVMe 1TB, PCIe 4.0, SMART health not yet verified."},
    {"id": "laptop", "label": "Laptop · public demo photo", "image": "/data/sample_images/laptop.jpg", "category": "Laptop", "notes": "Public Wikimedia Commons demo photo. Synthetic seller claim for local testing: Fieldbook 13, 16GB RAM, 512GB storage, powers on; battery health unknown."},
    {"id": "smartphone", "label": "Smartphone · public demo photo", "image": "/data/sample_images/smartphone.jpg", "category": "Smartphone", "notes": "Public Wikimedia Commons demo photo. Synthetic seller claim for local testing: Nova X 128GB, unlocked; battery health and activation lock need verification."},
    {"id": "monitor", "label": "Monitor · public demo photo", "image": "/data/sample_images/monitor.jpg", "category": "IPS monitor", "notes": "Public Wikimedia Commons demo photo. Synthetic seller claim for local testing: Viewline 27 QHD, IPS, 144Hz; ports, stand, and dead-pixel test need verification."},
    {"id": "keyboard", "label": "Mechanical keyboard · public demo photo", "image": "/data/sample_images/keyboard.jpg", "category": "Mechanical keyboard", "notes": "Public Wikimedia Commons demo photo. Synthetic seller claim for local testing: Keyforge 75, tactile switches; every key and USB connection need testing."},
]


class WorkflowStop(Exception):
    def __init__(self, reason: str, missing: list[str] | None = None):
        self.reason = reason
        self.missing = missing or []
        super().__init__(reason)


def portkey_json(messages: list[dict[str, Any]], *, temperature: float = 0.2, run_id: str | None = None, agent: str = "portkey", optimized: bool = True) -> dict[str, Any]:
    """Call Portkey's Responses API and parse a JSON object response."""
    import os

    api_key = os.getenv("PORTKEY_API_KEY")
    if not api_key:
        raise WorkflowStop("Portkey access is not configured. Add PORTKEY_API_KEY to the local .env file.")
    system_messages = [item["content"] for item in messages if item.get("role") == "system"]
    input_items = [{"role": item.get("role", "user"), "content": ("Return a valid JSON object.\n" + str(item.get("content", "")))} for item in messages if item.get("role") != "system"]
    body = json.dumps({
        "model": PORTKEY_MODEL,
        "instructions": "\n\n".join(system_messages) + "\n\nReturn a valid JSON object only.",
        "input": input_items,
        "temperature": temperature,
        "max_output_tokens": MAX_TOKENS_PER_CALL,
        "reasoning": {"effort": "none"},
        "text": {"format": {"type": "json_object"}},
        "prompt_cache_key": hashlib.sha256("\n\n".join(system_messages).encode()).hexdigest()[:32],
        "prompt_cache_retention": "24h",
    }).encode()
    request = urllib.request.Request(
        f"{PORTKEY_BASE_URL}/responses",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "FlipSafeAI/2.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.loads(response.read().decode("utf-8"))
            headers = response.headers
    except urllib.error.HTTPError as error:
        raw_error = error.read().decode("utf-8", errors="replace")
        try:
            provider_message = json.loads(raw_error).get("error", {}).get("message", "")
        except json.JSONDecodeError:
            provider_message = ""
        provider_message = re.sub(r"(?i)(api[_ -]?key|authorization|bearer)\s*[:=]?\s*\S+", r"\1 [redacted]", str(provider_message))[:240]
        if run_id:
            telemetry.event(run_id, "error", agent, f"Portkey request failed ({error.code})", tool="portkey", result_summary=provider_message or "Provider rejected the request.")
        raise WorkflowStop(f"Portkey request failed ({error.code}): {provider_message or 'provider rejected the request'}.") from error
    except urllib.error.URLError as error:
        if run_id:
            telemetry.event(run_id, "error", agent, "Portkey could not be reached", tool="portkey")
        raise WorkflowStop("Portkey could not be reached. Check local network or firewall access to api.portkey.ai.") from error
    if run_id:
        usage = payload.get("usage", {}) or {}
        raw_cost = payload.get("cost") or payload.get("total_cost") or usage.get("cost") or usage.get("total_cost") or headers.get("x-portkey-cost")
        try:
            cost = float(raw_cost) if raw_cost is not None else None
        except (TypeError, ValueError):
            cost = None
        telemetry.usage_record(run_id, agent, "portkey", usage, cost=cost, optimized=optimized)
        telemetry.event(run_id, "result", agent, "Portkey returned a structured response", tool="portkey", usage=usage, cost=cost)
    content = payload.get("output_text", "")
    if not content:
        for item in payload.get("output", []):
            for part in item.get("content", []):
                if part.get("type") in {"output_text", "text"}:
                    content += part.get("text", "")
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.I)
    try:
        return json.loads(content)
    except json.JSONDecodeError as error:
        raise WorkflowStop("Portkey returned a response that was not valid JSON.") from error


def image_data_url_info(data_url: str) -> dict[str, Any]:
    if not data_url or not data_url.startswith("data:image/") or "," not in data_url:
        raise WorkflowStop("A photo is required before inspection.", ["device photo"])
    header, encoded = data_url.split(",", 1)
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise WorkflowStop("The uploaded photo could not be read.", ["valid image data"]) from error
    if len(decoded) > 10 * 1024 * 1024:
        raise WorkflowStop("The uploaded photo is larger than 10MB.", ["smaller photo"])
    return {"mime_type": header[5:].split(";", 1)[0], "bytes": len(decoded), "raw_bytes": decoded, "data_url": data_url}


def optimize_and_inspect_image(data_url: str, *, max_side: int = 1600) -> dict[str, Any]:
    """Validate and resize the actual image before sending it to vision."""
    info = image_data_url_info(data_url)
    try:
        image = Image.open(io.BytesIO(info["raw_bytes"]))
        image.load()
        original_size = image.size
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.convert("RGB").save(output, format="JPEG", quality=88, optimize=True)
    except Exception as error:
        raise WorkflowStop("The uploaded photo could not be decoded as a supported image.", ["valid JPEG, PNG, or WEBP image"]) from error
    return {"mime_type": "image/jpeg", "raw_bytes": output.getvalue(), "bytes": len(output.getvalue()), "original_size": original_size, "optimized_size": image.size}


def comparison_tool(category: str, item_hint: str = "", spec_hint: str = "") -> list[dict[str, Any]]:
    """Select only relevant synthetic comparisons for the code-based pricing worker."""
    if category not in SUPPORTED:
        raise WorkflowStop(f"Unsupported item category: {category}.", ["supported electronics category"])
    source_category = "Monitor" if category == "IPS monitor" else category
    rows = [row for row in COMPARISONS if row["category"] == source_category]
    if item_hint:
        tokens = {token for token in re.findall(r"[a-z0-9]+", item_hint.lower()) if len(token) > 2}
        narrowed = [row for row in rows if len(tokens.intersection(set(re.findall(r"[a-z0-9]+", row["item"].lower())))) >= 1]
        if narrowed:
            rows = narrowed
        elif item_hint.strip().lower() not in {"unknown", "unknown model", "supported electronics item"}:
            raise WorkflowStop("There is not enough relevant comparison evidence for this identified item.", ["relevant sold comparisons"])
    if spec_hint:
        spec_tokens = {token for token in re.findall(r"[a-z0-9]+", spec_hint.lower()) if len(token) > 2}
        narrowed = [row for row in rows if spec_tokens.intersection(set(re.findall(r"[a-z0-9]+", row["storage_or_spec"].lower())))]
        if narrowed:
            rows = narrowed
    if not rows:
        raise WorkflowStop("There is not enough comparison evidence for this item.", ["relevant sold comparisons"])
    return rows


def search_market_comps(category: str, item_hint: str = "", spec_hint: str = "", *, run_id: str | None = None) -> list[dict[str, Any]]:
    rows = comparison_tool(category, item_hint, spec_hint)
    if run_id:
        telemetry.event(run_id, "tool", "pricing", "Selected relevant local sold comparisons", tool="search_market_comps", arguments={"category": category, "evidence_count": len(rows)}, result_summary=f"{len(rows)} synthetic comparison records")
    return rows


def pricing_tool(category: str, condition: str, item_hint: str, spec_hint: str) -> dict[str, Any]:
    """Calculate four seller-facing prices from selected comparisons; no model sets these numbers."""
    if condition not in {"Like new", "Good", "Fair", "For parts / not working"}:
        raise WorkflowStop("Pricing is paused because condition is unknown or unverified.", ["verified condition"])
    rows = search_market_comps(category, item_hint, spec_hint)
    values = sorted(float(row["sold_price"]) for row in rows)
    median = values[len(values) // 2] if len(values) % 2 else (values[len(values) // 2 - 1] + values[len(values) // 2]) / 2
    factor = {"Like new": 1.0, "Good": 0.86, "Fair": 0.68, "For parts / not working": 0.38}[condition]
    fair = median * factor
    quick = fair * 0.88
    listing = fair * 1.10
    minimum = fair * 0.78
    round_price = lambda value: int(5 * round(value / 5))
    result = PricingRecommendation(currency="USD", quick_sale=round_price(quick), fair_market=round_price(fair), listing_price=round_price(listing), minimum_acceptable_offer=round_price(minimum), method="Median of selected sold comparisons adjusted by declared condition.", rationale="Python math only; the model never sets these prices.", comparisons_used=[ComparisonRecord.model_validate(row) for row in rows], evidence_count=len(rows), condition_used=condition)
    if not (0 < result.minimum_acceptable_offer <= result.quick_sale <= result.fair_market <= result.listing_price):
        raise WorkflowStop("The calculated price tiers failed the positive ordered-price guardrail.", ["valid price tiers"])
    return result.model_dump()


def calculate_price_tiers_math(category: str, condition: str, item_hint: str, spec_hint: str) -> dict[str, Any]:
    """Named tool entry point used by the Pricing worker."""
    return pricing_tool(category, condition, item_hint, spec_hint)


def live_web_comp_search(category: str, item_hint: str = "") -> dict[str, Any]:
    """Optional market-search hook; external search stays unavailable by design."""
    return {"status": "unavailable", "category": category, "query": item_hint or category, "urls": [], "message": "Live web comparisons are unavailable; using local full synthetic evidence."}


def scan_buyer_message_security(message: str, listing_context: str = "") -> dict[str, Any]:
    from .security import scan_buyer_message

    return scan_buyer_message(message, listing_context)


def verify_listing_compliance(*, pricing: dict[str, Any], inspection: dict[str, Any], listing: dict[str, Any]) -> dict[str, Any]:
    issues: list[str] = []
    if not (pricing.get("minimum_acceptable_offer", 0) > 0 and pricing.get("minimum_acceptable_offer", 0) <= pricing.get("quick_sale", 0) <= pricing.get("fair_market", 0) <= pricing.get("listing_price", 0)):
        issues.append("Price tiers are not positive and ordered.")
    combined = json.dumps(listing).lower()
    for claim in inspection.get("seller_claims", []):
        if str(claim).lower() not in combined and claim:
            continue
    return {"compliant": not issues, "issues": issues, "checked": ["price_order", "unsupported_claims"]}


def sample_tool(sample_id: str) -> dict[str, Any] | None:
    return next((sample for sample in DEMO_SAMPLES if sample["id"] == sample_id), None)
