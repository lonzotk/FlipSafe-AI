from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from .agent import BossAgent
from .config import API_PROVIDER, FRONTEND_ORIGINS, PORTKEY_MODEL
from .models import AppraiseRequest, BuyerCheckRequest, SecurityScanRequest
from .telemetry import now, telemetry
from .security import attack_tests, limiter, scan_appraisal, scan_buyer_message
from .tools import COMPARISONS, DEMO_SAMPLES, PLAYBOOK, SUPPORTED, WorkflowStop

ROOT = Path(__file__).resolve().parents[1]
app = FastAPI(title="FlipSafe AI", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=FRONTEND_ORIGINS, allow_credentials=False, allow_methods=["*"], allow_headers=["*"])
boss = BossAgent()


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "configured": bool(os.getenv("PORTKEY_API_KEY")), "model": PORTKEY_MODEL, "provider": API_PROVIDER, "backend": "fastapi"}


@app.get("/api/demo-data")
def demo_data() -> dict:
    return {"synthetic": True, "supported_categories": sorted(SUPPORTED), "samples": DEMO_SAMPLES, "playbook": PLAYBOOK, "comparisons": COMPARISONS}


async def _run_appraisal(run_id: str, request: AppraiseRequest) -> None:
    try:
        result = await boss.appraise(**request.model_dump(), run_id=run_id)
        telemetry.finish(run_id, "stopped" if result["status"] == "stopped" else "complete", result)
    except Exception as error:
        telemetry.event(run_id, "error", "boss", f"Unhandled {error.__class__.__name__}", result_summary="The workflow failed; sensitive exception details were withheld.")
        telemetry.finish(run_id, "error", {"status": "error", "reason": "The appraisal workflow failed. Check the audit log."})


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "local"


def _blocked_run(kind: str, optimized: bool, findings: list[dict[str, str]], reason: str) -> dict[str, Any]:
    run_id = telemetry.create_run(kind, optimized)
    codes = sorted({finding["code"] for finding in findings})
    telemetry.event(run_id, "error", "security_gate", reason, blocked=True, finding_codes=codes)
    telemetry.safety_event({"at": now(), "run_id": run_id, "defenses": codes, "summary": reason})
    result = {"status": "blocked", "reason": reason, "security": {"allowed": False, "finding_codes": codes}}
    telemetry.finish(run_id, "blocked", result)
    return {"run_id": run_id, **result}


@app.post("/api/appraise")
async def appraise(request: AppraiseRequest, http_request: Request) -> JSONResponse:
    if not limiter.allow(_client_key(http_request)):
        return JSONResponse({"status": "blocked", "reason": "Request limit reached. Try again in a minute.", "security": {"finding_codes": ["REQUEST_LIMIT"]}}, status_code=429)
    security = scan_appraisal(notes=request.notes, buyer_message=request.buyer_message, category=request.category)
    if not security["allowed"]:
        return JSONResponse(_blocked_run("security_block", request.optimized, security["findings"], "Security gate blocked untrusted instructions or manipulated evidence."), status_code=422)
    request = request.model_copy(update={"notes": security["sanitized"]["notes"], "buyer_message": security["sanitized"]["buyer_message"]})
    run_id = telemetry.create_run("appraisal", request.optimized)
    asyncio.create_task(_run_appraisal(run_id, request))
    return JSONResponse({"status": "queued", "run_id": run_id}, status_code=202)


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str) -> JSONResponse:
    if not telemetry.cancel(run_id):
        return JSONResponse({"status": "not_cancellable", "run_id": run_id}, status_code=409)
    return JSONResponse({"status": "cancellation_requested", "run_id": run_id})


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str) -> StreamingResponse:
    snapshot = telemetry.snapshot(run_id)
    if not snapshot:
        return StreamingResponse(iter(["event: error\ndata: {\"status\":\"not_found\"}\n\n"]), media_type="text/event-stream", status_code=404)
    queue, initial = telemetry.subscribe(run_id)

    async def stream():
        try:
            for event in initial:
                yield f"data: {json.dumps(event)}\n\n"
            if snapshot["status"] in {"complete", "stopped", "error", "blocked", "cancelled"}:
                done = {"status": snapshot["status"], "run_id": run_id}
                yield f"event: done\ndata: {json.dumps(done)}\n\n"
                return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(event)}\n\n"
                    latest = telemetry.snapshot(run_id)
                    if latest and latest["status"] in {"complete", "stopped", "error", "blocked", "cancelled"}:
                        done = {"status": latest["status"], "run_id": run_id}
                        yield f"event: done\ndata: {json.dumps(done)}\n\n"
                        return
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            telemetry.unsubscribe(run_id, queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/tokenomics/compare")
async def compare_tokenomics(request: AppraiseRequest, http_request: Request) -> JSONResponse:
    if not limiter.allow(_client_key(http_request)):
        return JSONResponse({"status": "blocked", "reason": "Request limit reached. Try again in a minute.", "security": {"finding_codes": ["REQUEST_LIMIT"]}}, status_code=429)
    security = scan_appraisal(notes=request.notes, buyer_message=request.buyer_message, category=request.category)
    if not security["allowed"]:
        return JSONResponse(_blocked_run("security_block", request.optimized, security["findings"], "Security gate blocked untrusted instructions or manipulated evidence."), status_code=422)
    request = request.model_copy(update={"notes": security["sanitized"]["notes"], "buyer_message": security["sanitized"]["buyer_message"]})
    optimized_id = telemetry.create_run("tokenomics_comparison", True)
    unoptimized_id = telemetry.create_run("tokenomics_comparison", False)
    optimized_request = request.model_copy(update={"optimized": True})
    unoptimized_request = request.model_copy(update={"optimized": False})
    asyncio.create_task(_run_appraisal(optimized_id, optimized_request))
    asyncio.create_task(_run_appraisal(unoptimized_id, unoptimized_request))
    return JSONResponse({"status": "queued", "optimized_run_id": optimized_id, "unoptimized_run_id": unoptimized_id}, status_code=202)


@app.get("/api/runs/{run_id}")
def run_status(run_id: str) -> JSONResponse:
    result = telemetry.snapshot(run_id)
    return JSONResponse(result or {"status": "not_found"}, status_code=200 if result else 404)


@app.get("/api/tokenomics")
def tokenomics() -> dict:
    return telemetry.summary()


@app.get("/api/audit-log")
def audit_log() -> dict:
    return {"runs": telemetry.audit_log(), "summary": telemetry.summary()}


@app.get("/api/security/attack-tests")
def security_attack_tests() -> dict:
    return {"synthetic": True, "tests": attack_tests()}


@app.post("/api/security/scan")
def security_scan(request: SecurityScanRequest) -> JSONResponse:
    result = scan_buyer_message(request.text) if request.context == "buyer_message" else scan_appraisal(notes=request.text, buyer_message="", category="security-test")
    findings = result["findings"]
    audit = _blocked_run("security_test", True, findings, "Visible synthetic security test completed.") if findings else {"status": "allowed", "security": {"allowed": True}}
    return JSONResponse({"status": "blocked" if findings else "allowed", "allowed": result["allowed"], "findings": findings, "audit": audit})


async def _run_buyer_check(run_id: str, request: BuyerCheckRequest) -> None:
    try:
        result = await boss.buyer_check(message=request.message, listing_context=request.listing_context, run_id=run_id, optimized=request.optimized)
        telemetry.finish(run_id, "complete", result)
    except WorkflowStop as error:
        telemetry.event(run_id, "error", "boss", error.reason)
        telemetry.finish(run_id, "stopped", {"status": "stopped", "reason": error.reason})
    except Exception:
        telemetry.event(run_id, "error", "boss", "Buyer safety workflow failed")
        telemetry.finish(run_id, "error", {"status": "error", "reason": "The buyer safety workflow failed. Check the audit log."})


@app.post("/api/check-message")
async def check_message(request: BuyerCheckRequest, http_request: Request) -> JSONResponse:
    if not limiter.allow(_client_key(http_request)):
        return JSONResponse({"status": "blocked", "reason": "Request limit reached. Try again in a minute.", "security": {"finding_codes": ["REQUEST_LIMIT"]}}, status_code=429)
    security = scan_buyer_message(request.message, request.listing_context)
    if not security["allowed"]:
        return JSONResponse(_blocked_run("security_block", request.optimized, security["findings"], "Security gate blocked a phishing or instruction-like buyer message."), status_code=422)
    request = request.model_copy(update={"message": security["sanitized"]["message"], "listing_context": security["sanitized"]["listing_context"]})
    run_id = telemetry.create_run("buyer_check", request.optimized)
    asyncio.create_task(_run_buyer_check(run_id, request))
    return JSONResponse({"status": "queued", "run_id": run_id}, status_code=202)


@app.exception_handler(WorkflowStop)
async def workflow_stop_handler(_request, error: WorkflowStop) -> JSONResponse:
    return JSONResponse({"status": "stopped", "reason": error.reason, "missing_evidence": error.missing}, status_code=422)


app.mount("/data", StaticFiles(directory=str(ROOT / "data")), name="data")
FRONTEND = ROOT / "frontend" / "dist"
if not FRONTEND.exists():
    FRONTEND = ROOT / "frontend"
app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")
