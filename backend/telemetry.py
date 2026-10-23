"""Run events, SSE queues, measured usage, limits, and redacted output logs."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import MAX_REQUESTS_PER_AGENT, MAX_SPEND_PER_MONTH_USD, MAX_SPEND_PER_RUN_USD, MAX_TOKENS_PER_RUN

OUTPUT = Path(__file__).resolve().parents[1] / "output"
AUDIT_PATH = OUTPUT / "audit_trail.json"
METRICS_PATH = OUTPUT / "token_metrics.json"
SAFETY_PATH = OUTPUT / "safety_events.json"

# gpt-6-luna cost is not returned by every Portkey response in this setup.
# These are transparent planning rates, not an invoice or provider quote.
ESTIMATED_INPUT_USD_PER_MILLION = 0.50
ESTIMATED_CACHED_INPUT_USD_PER_MILLION = 0.05
ESTIMATED_OUTPUT_USD_PER_MILLION = 1.50


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _number(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _get(value: Any, *names: str) -> Any:
    for name in names:
        if isinstance(value, dict) and value.get(name) is not None:
            return value[name]
        candidate = getattr(value, name, None)
        if candidate is not None:
            return candidate
    return None


class TelemetryStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.runs: dict[str, dict[str, Any]] = {}
        self.usage: list[dict[str, Any]] = []
        self._instructions: dict[str, str] = {}
        self.cache_hits = 0
        self.cache_misses = 0
        self._subscribers: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]]] = {}
        self._restore_persisted_metrics()

    def _restore_persisted_metrics(self) -> None:
        """Restore redacted usage counters so Tokenomics survives restarts."""
        try:
            payload = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        records = payload.get("records", []) if isinstance(payload, dict) else []
        if not isinstance(records, list):
            return
        for record in records:
            if not isinstance(record, dict) or not record.get("run_id"):
                continue
            restored = {
                "run_id": str(record.get("run_id")),
                "agent": str(record.get("agent", "unknown")),
                "source": str(record.get("source", "unknown")),
                "input_tokens": _number(record.get("input_tokens")),
                "cached_input_tokens": _number(record.get("cached_input_tokens")),
                "cache_write_tokens": _number(record.get("cache_write_tokens")),
                "output_tokens": _number(record.get("output_tokens")),
                "total_tokens": _number(record.get("total_tokens")),
                "cost_usd": record.get("cost_usd") if isinstance(record.get("cost_usd"), (int, float)) else None,
                "cost_status": "measured" if isinstance(record.get("cost_usd"), (int, float)) else "pending",
                "optimized": bool(record.get("optimized", True)),
                "at": str(record.get("at", "")),
            }
            self.usage.append(restored)
            run_id = restored["run_id"]
            run = self.runs.setdefault(run_id, {"run_id": run_id, "kind": "persisted", "optimized": restored["optimized"], "status": "complete", "events": [], "usage": [], "result": None, "cancel_requested": False, "created_at": restored["at"]})
            run["usage"].append(restored)
        saved_summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
        cache = saved_summary.get("instruction_cache", {}) if isinstance(saved_summary, dict) else {}
        if isinstance(cache, dict):
            self.cache_hits = int(cache.get("hits", 0) or 0)
            self.cache_misses = int(cache.get("misses", 0) or 0)

    def create_run(self, kind: str, optimized: bool = True) -> str:
        run_id = str(uuid.uuid4())
        with self._lock:
            self.runs[run_id] = {"run_id": run_id, "kind": kind, "optimized": optimized, "status": "queued", "events": [], "usage": [], "result": None, "cancel_requested": False, "created_at": now()}
        return run_id

    def event(self, run_id: str, event_type: str, agent: str, message: str, **extra: Any) -> None:
        event = {"at": now(), "type": event_type, "agent": agent, "message": message, **extra}
        with self._lock:
            run = self.runs.get(run_id)
            subscribers = list(self._subscribers.get(run_id, set()))
            if run:
                run["events"].append(event)
                if event_type == "start":
                    run["status"] = "running"
                elif event_type == "error" and run["status"] not in {"cancelled", "complete"}:
                    run["status"] = "error"
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                continue

    def subscribe(self, run_id: str) -> tuple[asyncio.Queue[dict[str, Any]], list[dict[str, Any]]]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        with self._lock:
            run = self.runs.get(run_id)
            if not run:
                return queue, []
            self._subscribers.setdefault(run_id, set()).add((loop, queue))
            initial = copy.deepcopy(run["events"])
        return queue, initial

    def unsubscribe(self, run_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            subscribers = {item for item in self._subscribers.get(run_id, set()) if item[1] is not queue}
            if subscribers:
                self._subscribers[run_id] = subscribers
            else:
                self._subscribers.pop(run_id, None)

    def finish(self, run_id: str, status: str, result: dict[str, Any] | None = None) -> None:
        with self._lock:
            if run_id in self.runs:
                self.runs[run_id]["status"] = status
                self.runs[run_id]["result"] = result
                self.runs[run_id]["finished_at"] = now()
        self._persist_outputs()

    def cancel(self, run_id: str) -> bool:
        with self._lock:
            run = self.runs.get(run_id)
            if not run or run["status"] in {"complete", "error", "stopped", "cancelled", "blocked"}:
                return False
            run["cancel_requested"] = True
        self.event(run_id, "cancelled", "boss", "Cancellation requested; the next safe handoff will stop.")
        return True

    def is_cancelled(self, run_id: str) -> bool:
        with self._lock:
            return bool(self.runs.get(run_id, {}).get("cancel_requested"))

    def usage_record(self, run_id: str, agent: str, source: str, usage: Any, *, cost: float | None = None, optimized: bool = True) -> dict[str, Any]:
        details = _get(usage, "details") or {}
        if not isinstance(details, dict):
            details = {}
        input_tokens = _number(_get(usage, "input_tokens", "request_tokens", "prompt_tokens"))
        output_tokens = _number(_get(usage, "output_tokens", "response_tokens", "completion_tokens"))
        cached_input_tokens = _number(details.get("cached_tokens") or details.get("cache_read_tokens") or _get(usage, "cached_input_tokens"))
        cache_write_tokens = _number(details.get("cache_write_tokens") or _get(usage, "cache_write_tokens"))
        total_tokens = _number(_get(usage, "total_tokens"))
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
        record = {"run_id": run_id, "agent": agent, "source": source, "input_tokens": input_tokens, "cached_input_tokens": cached_input_tokens, "cache_write_tokens": cache_write_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens, "cost_usd": cost, "cost_status": "measured" if cost is not None else "pending", "optimized": optimized, "at": now()}
        with self._lock:
            self.usage.append(record)
            if run_id in self.runs:
                self.runs[run_id]["usage"].append(record)
        self._persist_metrics()
        return record

    def instruction(self, name: str, prompt: str, optimized: bool) -> str:
        key = f"{name}:{optimized}:{hashlib.sha256(prompt.encode()).hexdigest()}"
        with self._lock:
            if key in self._instructions:
                self.cache_hits += 1
                return self._instructions[key]
            self.cache_misses += 1
            value = prompt if optimized else f"{prompt}\n\nRepeat the complete rules above and restate every constraint before responding."
            self._instructions[key] = value
            return value

    def limit_status(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            records = [record for record in self.usage if record["run_id"] == run_id]
            run = self.runs.get(run_id, {})
        tokens = sum(record["total_tokens"] or 0 for record in records)
        costs = [record["cost_usd"] for record in records if record["cost_usd"] is not None]
        cost = sum(costs) if costs else None
        agent_counts: dict[str, int] = {}
        for record in records:
            agent_counts[record["agent"]] = agent_counts.get(record["agent"], 0) + 1
        limited_agent = next((agent for agent, count in agent_counts.items() if count >= MAX_REQUESTS_PER_AGENT), None)
        if limited_agent:
            return {"allowed": False, "code": "AGENT_REQUEST_LIMIT", "message": f"The {limited_agent} request limit was reached.", "tokens": tokens, "cost": cost}
        if tokens > MAX_TOKENS_PER_RUN:
            return {"allowed": False, "code": "TOKEN_LIMIT", "message": "The run exceeded its token limit.", "tokens": tokens, "cost": cost}
        if cost is not None and cost > MAX_SPEND_PER_RUN_USD:
            return {"allowed": False, "code": "SPEND_LIMIT", "message": "The run exceeded its spending limit.", "tokens": tokens, "cost": cost}
        month_cost = sum(record["cost_usd"] or 0 for record in self.usage)
        if month_cost > MAX_SPEND_PER_MONTH_USD:
            return {"allowed": False, "code": "MONTHLY_SPEND_LIMIT", "message": "The monthly spending limit has been reached.", "tokens": tokens, "cost": cost}
        if run.get("cancel_requested"):
            return {"allowed": False, "code": "CANCELLED", "message": "The run was cancelled.", "tokens": tokens, "cost": cost}
        return {"allowed": True, "tokens": tokens, "cost": cost}

    def snapshot(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self.runs.get(run_id)
            return copy.deepcopy(run) if run else None

    def audit_log(self) -> list[dict[str, Any]]:
        with self._lock:
            result = copy.deepcopy(list(self.runs.values()))
        allowed = {"at", "type", "agent", "message", "tool", "usage", "cost", "cost_usd", "blocked", "finding_codes", "missing_evidence", "evidence_count", "prices", "mode", "arguments", "result_summary"}
        for run in result:
            run.pop("result", None)
            run.pop("cancel_requested", None)
            for event in run.get("events", []):
                for key in list(event):
                    if key not in allowed:
                        event.pop(key, None)
        return result

    def safety_event(self, event: dict[str, Any]) -> None:
        existing = self._read_json(SAFETY_PATH, {"events": []})
        rows = existing.get("events", []) if isinstance(existing, dict) else []
        rows.append(event)
        self._write_json(SAFETY_PATH, {"events": rows})

    def summary(self) -> dict[str, Any]:
        with self._lock:
            records = list(self.usage)
            runs = list(self.runs.values())
            cache = {"hits": self.cache_hits, "misses": self.cache_misses, "entries": len(self._instructions)}
        groups: dict[str, list[dict[str, Any]]] = {"optimized": [], "unoptimized": []}
        for record in records:
            groups["optimized" if record["optimized"] else "unoptimized"].append(record)

        def aggregate(items: list[dict[str, Any]]) -> dict[str, Any]:
            measured_tokens = [item["total_tokens"] for item in items if item["total_tokens"] is not None]
            measured_costs = [item["cost_usd"] for item in items if item["cost_usd"] is not None]
            run_totals: dict[str, int] = {}
            run_costs: dict[str, float] = {}
            estimated_run_costs: dict[str, float] = {}
            run_complete: dict[str, bool] = {}
            for item in items:
                run_id = item["run_id"]
                if item["total_tokens"] is not None:
                    run_totals[run_id] = run_totals.get(run_id, 0) + item["total_tokens"]
                run_complete.setdefault(run_id, True)
                if item["cost_usd"] is None:
                    run_complete[run_id] = False
                    input_tokens = item.get("input_tokens") or 0
                    cached_tokens = min(item.get("cached_input_tokens") or 0, input_tokens)
                    output_tokens = item.get("output_tokens") or 0
                    if input_tokens or output_tokens:
                        billable_input = max(input_tokens - cached_tokens, 0)
                        estimate = (
                            billable_input * ESTIMATED_INPUT_USD_PER_MILLION
                            + cached_tokens * ESTIMATED_CACHED_INPUT_USD_PER_MILLION
                            + output_tokens * ESTIMATED_OUTPUT_USD_PER_MILLION
                        ) / 1_000_000
                        estimated_run_costs[run_id] = estimated_run_costs.get(run_id, 0.0) + estimate
                else:
                    run_costs[run_id] = run_costs.get(run_id, 0.0) + item["cost_usd"]
                    estimated_run_costs[run_id] = estimated_run_costs.get(run_id, 0.0) + item["cost_usd"]
            measured_run_totals = list(run_totals.values())
            complete_costs = [run_costs[key] for key, complete in run_complete.items() if complete and key in run_costs]
            estimated_costs = list(estimated_run_costs.values())
            if complete_costs and len(complete_costs) == len(run_complete):
                cost_status = "measured"
            elif estimated_costs:
                cost_status = "estimated"
            else:
                cost_status = "pending"
            return {
                "calls": len(items),
                "measured_calls": len(measured_tokens),
                "measured_runs": len(measured_run_totals),
                "total_tokens": sum(measured_tokens) if measured_tokens else None,
                "average_tokens_per_run": round(sum(measured_run_totals) / len(measured_run_totals), 2) if measured_run_totals else None,
                "total_cost": round(sum(measured_costs), 8) if measured_costs else None,
                "average_cost_per_run": round(sum(complete_costs) / len(complete_costs), 8) if complete_costs else None,
                "estimated_total_cost": round(sum(estimated_costs), 8) if estimated_costs else None,
                "average_estimated_cost_per_run": round(sum(estimated_costs) / len(estimated_costs), 8) if estimated_costs else None,
                "cost_status": cost_status,
            }

        aggregates = {name: aggregate(items) for name, items in groups.items()}
        projections: dict[str, Any] = {}
        for users in (1, 100, 10000):
            projections[str(users)] = {}
            for mode, data in aggregates.items():
                average_cost = data["average_cost_per_run"] if data["cost_status"] == "measured" else data["average_estimated_cost_per_run"]
                projections[str(users)][mode] = {"monthly_users": users, "tokens": round(data["average_tokens_per_run"] * users) if data["average_tokens_per_run"] is not None else None, "cost": round(average_cost * users, 8) if average_cost is not None else None, "cost_status": data["cost_status"] if data["average_tokens_per_run"] is not None else "pending"}
        return {"runs": len(runs), "usage_records": len(records), "optimized": aggregates["optimized"], "unoptimized": aggregates["unoptimized"], "projections": projections, "instruction_cache": cache, "pricing_assumption": "Measured Portkey costs are used when available. Otherwise, estimates use $0.50 / 1M input tokens, $0.05 / 1M cached input tokens, and $1.50 / 1M output tokens."}

    def _read_json(self, path: Path, default: dict[str, Any]) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else default
        except (OSError, json.JSONDecodeError):
            return default

    def _write_json(self, path: Path, value: dict[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value, indent=2), encoding="utf-8")
        except OSError:
            return

    def _persist_outputs(self) -> None:
        current = self.audit_log()
        existing = self._read_json(AUDIT_PATH, {"runs": []})
        historical = existing.get("runs", []) if isinstance(existing.get("runs", []), list) else []
        merged = {row.get("run_id"): row for row in historical if row.get("run_id")}
        merged.update({row.get("run_id"): row for row in current if row.get("run_id")})
        self._write_json(AUDIT_PATH, {"runs": list(merged.values()), "summary": self.summary()})

    def _persist_metrics(self) -> None:
        existing = self._read_json(METRICS_PATH, {"records": []})
        historical = existing.get("records", []) if isinstance(existing.get("records", []), list) else []
        merged = {f"{row.get('run_id')}:{row.get('agent')}:{row.get('at')}": row for row in historical}
        with self._lock:
            current = list(self.usage)
        merged.update({f"{row.get('run_id')}:{row.get('agent')}:{row.get('at')}": row for row in current})
        self._write_json(METRICS_PATH, {"records": list(merged.values()), "summary": self.summary()})


telemetry = TelemetryStore()
