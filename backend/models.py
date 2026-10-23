"""API, worker, and audit data models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ComparisonRecord(BaseModel):
    id: str
    category: str
    item: str
    condition: str
    storage_or_spec: str
    sold_price: float = Field(gt=0)
    days_listed: int = Field(ge=0)


class VisionAppraisalResult(BaseModel):
    item_name: str = Field(description="Exact model only when supported by the image or notes; otherwise Unknown")
    category: str
    supported: bool
    visible_observations: list[str] = Field(default_factory=list)
    visible_damage: list[str] = Field(default_factory=list)
    condition_estimate: str = "Unknown"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    verified_specs: list[str] = Field(default_factory=list)
    seller_claims: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    defects: list[str] = Field(default_factory=list)
    selling_points: list[str] = Field(default_factory=list)
    missing_questions: list[str] = Field(default_factory=list)


# Backward-compatible import name for integrations built against the first version.
PhotoInspectionResult = VisionAppraisalResult


class PricingRecommendation(BaseModel):
    currency: str = "USD"
    quick_sale: float = Field(gt=0)
    fair_market: float = Field(gt=0)
    listing_price: float = Field(gt=0)
    minimum_acceptable_offer: float = Field(gt=0)
    comparisons_used: list[ComparisonRecord] = Field(default_factory=list)
    evidence_count: int = Field(ge=0)
    method: str
    rationale: str = ""
    condition_used: str


class BuyerSafetyVerdict(BaseModel):
    risk: Literal["SAFE", "SUSPICIOUS", "BLOCKED_ATTACK", "not_checked"] = "not_checked"
    threats: list[str] = Field(default_factory=list)
    signals: list[str] = Field(default_factory=list)
    recommendation: str
    suggested_reply: str = ""


class ListingAndSafetyResult(BaseModel):
    listing: dict[str, Any] = Field(default_factory=dict)
    title: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    negotiation_guidance: list[str] = Field(default_factory=list)
    copy_ready_listing: str = ""
    safety: BuyerSafetyVerdict = Field(default_factory=lambda: BuyerSafetyVerdict(recommendation="No buyer message was supplied."))
    uncertainties: list[str] = Field(default_factory=list)


class FlipSafeAppraisalPackage(BaseModel):
    status: Literal["ready", "stopped", "blocked", "error"]
    run_id: str = ""
    category: str = ""
    inspection: VisionAppraisalResult | None = None
    pricing: PricingRecommendation | None = None
    listing_and_safety: ListingAndSafetyResult | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list)
    reason: str = ""
    missing_evidence: list[str] = Field(default_factory=list)
    usage: list[dict[str, Any]] = Field(default_factory=list)


class AppraiseRequest(BaseModel):
    image: str = Field(min_length=1)
    category: str = "auto"
    condition: str = ""
    notes: str = ""
    buyer_message: str = ""
    sample_id: str = ""
    optimized: bool = True


class BuyerCheckRequest(BaseModel):
    message: str = Field(min_length=1)
    listing_context: str = ""
    optimized: bool = True


class SecurityScanRequest(BaseModel):
    text: str = Field(min_length=1)
    context: str = "buyer_message"


class CancelResponse(BaseModel):
    status: str
    run_id: str
