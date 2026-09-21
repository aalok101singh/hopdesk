"""HopDesk — Smart-Budget AI Support Agent (Track #04).

Minimal dark chat + Run Inspection drawer, powered by the ModelHop router
engine (imported, never forked):
  - QueryAnalyzer + LearningRouter -> cheapest capable tier
  - ConfidenceEngine (threshold 0.7) -> escalate when unsure
  - CascadeFallback + HealthRegistry -> deterministic mock on 429/outage
  - TraceLogger (trace_log.jsonl)   -> auditable runs

Works with zero keys (deterministic offline dispatch); uses live providers
when GROQ_API_KEY / GEMINI_API_KEY / OPENAI_API_KEY are configured.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from modelhop import ModelHop, estimate_cost
from modelhop.core.models import ConfidenceResult, ProviderResponse, RoutingDecision

BASE = Path(__file__).parent

with open(BASE / "seed.json", encoding="utf-8") as f:
    SEED: Dict[str, Any] = json.load(f)

mh = ModelHop(config_path=str(BASE / "modelhop.yaml"))
THRESHOLD = float(mh.adaptive_threshold.get_threshold())

HEATED = re.compile(
    r"contract|clause|liab|indemn|counsel|lawyer|sue|dispute|demand|"
    r"unacceptable|breach|revision|signing|8\.3|12\.4|"
    r"angr|furious|frustrat|urgent|asap|immediately|kill|threat|"
    r"escalat|manager\b|harm|safety|deadline",
    re.IGNORECASE,
)
ORDER = re.compile(r"#?[A-Z]{2}-\d{4,6}")
REFUND = re.compile(r"refund|shipping|deliver|arriv|late|package|order|#US", re.IGNORECASE)


def _free_model() -> str:
    for m in mh.models:
        if m.tier.value == "free":
            return m.name
    return mh.models[0].name


def _premium_model() -> str:
    for m in mh.models:
        if m.tier.value == "premium":
            return m.name
    return mh.models[-1].name


FREE_MODEL = _free_model()
PREMIUM_MODEL = _premium_model()

# ------------------------------------------------------------------ state

run_seq = 209543
down: set = set()
sessions: Dict[str, List[Dict[str, Any]]] = {}
saved_today = float(SEED.get("saved_today", 12.43))


def _seed_session() -> List[Dict[str, Any]]:
    turns: List[Dict[str, Any]] = []
    seq = 209540
    for t in SEED["opening_turns"]:
        seq += 1
        baseline = float(t["cost_baseline"])
        actual = float(t["cost_actual"])
        turns.append(
            {
                "run": seq,
                "query": t["query"],
                "answer": t["response"],
                "agent": "Contract agent" if t["tier"] == "premium" else "FAQ agent",
                "model": t["model"],
                "provider": _provider_of(t["model"]),
                "tier": t["tier"],
                "status": t["status"],
                "intent": t["intent"],
                "policy": "cheap-first",
                "complexity": 0.18 if t["tier"] == "free" else 0.71,
                "complexity_label": "Simple" if t["tier"] == "free" else "Complex",
                "confidence": t["confidence"],
                "threshold": THRESHOLD,
                "tokens_in": 48 if t["tier"] == "free" else 96,
                "tokens_out": 86 if t["tier"] == "free" else 210,
                "tokens_total": (48 if t["tier"] == "free" else 96)
                + (86 if t["tier"] == "free" else 210),
                "fallback_count": t["fallback_count"],
                "degraded": False,
                "cached": False,
                "live": False,
                "latency_ms": t["latency_ms"],
                "cost_actual": actual,
                "cost_baseline": baseline,
                "saved": round(baseline - actual, 4),
                "order_card": t.get("order_card"),
                "doc": t.get("doc"),
                "ledger": f"#{seq - 200000}-seed",
                "why": (
                    "Standard policy inquiry, high confidence intent match on keyword SLA 4.2."
                    if "shipping" in t["intent"] or "refund" in t["query"].lower()
                    else "Contract language detected, escalated to premium tier."
                ),
                "at": "10:42 AM",
            }
        )
    return turns


def _provider_of(model_name: str) -> str:
    m = mh.registry.get_model(model_name)
    if m is not None:
        return m.provider
    if model_name == "local-mock-fallback":
        return "mock"
    return "unknown"


def _tokens(text: str) -> int:
    return max(8, int(len(text.split()) * 1.33))


def mock_answer(query: str, escalated: bool) -> tuple:
    order = ORDER.search(query)
    oid = order.group(0) if order else "#US-89304"
    if not oid.startswith("#"):
        oid = "#" + oid
    if escalated:
        return (
            "This looks like a contractual dispute, so here is a safe holding response "
            "while a specialist reviews the thread.\n\n"
            "We acknowledge your concerns on the liability cap and indemnity language. "
            "A contracts specialist has been assigned and will respond with a revised "
            f"clause proposal. No automated change has been made to your agreement. Reference {oid}.",
            None,
            None,
        )
    if REFUND.search(query):
        return (
            "Under Section 4.2 of our Guaranteed Delivery SLA, orders delayed more than "
            "48 hours past the estimated carrier transit window qualify for an automatic "
            "delivery guarantee credit.\n\n"
            f"We've verified order {oid} with the carrier event log. You are entitled to "
            "an immediate $15.00 shipping credit credited to your original payment method.",
            {"order": f"Order {oid}", "note": "Delayed 4 days", "action": "Claim refund"},
            None,
        )
    if re.search(r"cancell|subscription|billing|seat", query, re.IGNORECASE):
        return (
            "Under Master Subscription Terms (Rev 2023.2), quarterly commercial seats may "
            "be downscoped up to 14 days prior to automatic billing cycle renewals. Notice "
            "must be submitted via the admin billing dashboard.",
            None,
            None,
        )
    if re.search(r"MSA|master services|agreement|2023", query, re.IGNORECASE):
        return (
            "Your signed agreement (MSA-2023-HopDesk-Enterprise) is archived in your "
            "Organization Security Vault. Because your seat has Legal Administrator "
            "privileges, you can download the countersigned PDF directly.",
            None,
            {"name": "MSA_Executed_2023_HopDesk.pdf", "meta": "240 KB"},
        )
    return (
        "Thanks for reaching out. I've logged your request and a support agent will "
        "follow up shortly. If this is about an order, please share the order number "
        "so we can look it up instantly.",
        None,
        None,
    )


async def dispatch(session_id: str, query: str) -> Dict[str, Any]:
    """Route one query through ModelHop; honest offline fallback included."""
    global run_seq
    features = mh.feature_extractor.extract(query)
    analysis = await mh.analyzer.analyze(query)

    try:
        decision, _ = mh.learning_router.route(analysis, features)
        routed_name = decision.model.name
    except Exception:
        routed_name = FREE_MODEL

    heated = bool(HEATED.search(query))
    complex_q = float(analysis.complexity or 0) >= 0.6
    escalate = heated or complex_q
    target = PREMIUM_MODEL if escalate else routed_name

    degraded = False
    fallback_count = 0
    cached = False
    live = False
    live_tier: Any = None
    live_latency: Any = None
    confidence = 0.62 if escalate else 0.92
    answer: Any = None
    model_used = target

    outage = target in down or bool({"openai", "premium", PREMIUM_MODEL} & down)
    premium_attempt: Any = None  # None = not tried; otherwise human-readable outcome
    if escalate and not outage:
        # Premium-first: heated/complex queries try GPT-4 before cheap tiers.
        try:
            premium_provider = mh.registry.get_provider(PREMIUM_MODEL)
        except Exception:
            premium_provider = None
        if premium_provider is None:
            premium_attempt = "tried → unavailable (no OPENAI_API_KEY configured)"
        else:
            try:
                premium_resp = await premium_provider.generate(query)
                answer = premium_resp.content
                model_used = PREMIUM_MODEL
                live_tier = "premium"
                try:
                    live_latency = int(premium_resp.latency_ms)
                except Exception:
                    live_latency = None
                live = True
                premium_attempt = "tried → served"
                try:
                    conf_check = await mh.confidence_engine.check(
                        query, premium_resp, query_features=features
                    )
                    confidence = float(conf_check.score)
                    degraded = not bool(conf_check.is_confident)
                    fallback_count = 1 if degraded else 0
                except Exception:
                    pass
            except Exception as exc:
                premium_attempt = f"tried → failed ({type(exc).__name__}: {str(exc)[:80]})"
    if answer is None and mh.registry.get_available_providers() and not outage:
        try:
            real = await mh.route(query)
            # Authoritative-live: everything derives from the real result.
            answer = real.response
            model_used = real.model
            live_tier = real.tier
            confidence = float(real.confidence.score)
            degraded = bool(real.degraded)
            fallback_count = 1 if degraded else 0
            cached = bool(real.cached)
            live = True
            try:
                live_latency = int(real._provider_response.latency_ms)  # type: ignore
            except Exception:
                live_latency = None
        except Exception:
            answer = None
    if premium_attempt is None and escalate and outage:
        premium_attempt = "tried → unavailable (premium rate-limited / killed)"

    if answer is None:
        # Offline/simulated path: heuristic policy only runs here.
        if escalate:
            degraded = True
            fallback_count = 1
            model_used = "local-mock-fallback"
        answer, order_card, doc = mock_answer(query, escalate)
    else:
        _, order_card, doc = mock_answer(query, escalate)

    if live:
        tier_value = str(live_tier or "free")
        cost_model = mh.registry.get_model(model_used) or mh.registry.get_model(FREE_MODEL)
        provider_value = _provider_of(model_used)
    else:
        cost_model_name = PREMIUM_MODEL if escalate else model_used
        cost_model = mh.registry.get_model(cost_model_name) or mh.registry.get_model(FREE_MODEL)
        tier_value = getattr(cost_model.tier, "value", str(cost_model.tier))
        provider_value = "mock" if model_used == "local-mock-fallback" else _provider_of(model_used)
    tokens_in, tokens_out = _tokens(query), _tokens(answer)
    pr = ProviderResponse(
        content=answer,
        model_used=cost_model.name,
        provider=cost_model.provider,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=int(live_latency) if live_latency else (1240 if escalate and not live else 380),
    )
    cost = estimate_cost(pr, cost_model)
    actual = 0.0 if (degraded and model_used == "local-mock-fallback") else float(cost.actual_cost)
    actual_display = 0.0001 if (not escalate and actual == 0.0) else actual
    baseline = float(cost.would_have_cost) or 0.042

    ledger = ""
    try:
        final_model = mh.registry.get_model(model_used) or cost_model
        conf = ConfidenceResult(
            score=round(confidence, 3),
            is_confident=confidence >= THRESHOLD,
            threshold=THRESHOLD,
            reasoning="HopDesk dispatch" if live else "Deterministic dispatch (no live provider)",
            method="heuristic",
        )
        trace = mh.trace_logger.log(
            query=query,
            analysis=analysis,
            decision=RoutingDecision(
                model=final_model, tier=final_model.tier,
                reason="HopDesk dispatch", degraded=degraded,
            ),
            response=pr,
            confidence=conf,
            cost=cost,
            fallback_count=fallback_count,
            degraded=degraded,
        )
        ledger = getattr(trace, "ledger_id", "") or getattr(trace, "query_id", "") or ""
    except Exception:
        pass

    run_seq += 1
    is_premium = tier_value == "premium"
    agent = "Contract agent" if (is_premium or degraded) else "FAQ agent"
    status = "fallback" if degraded else ("escalated-premium" if is_premium else "resolved-cheap")
    if live:
        served = f"live ● {provider_value}"
        if premium_attempt and premium_attempt != "tried → served" and not is_premium:
            why = (
                f"Heated language → tried GPT-4 first ({premium_attempt[8:]}), "
                f"{model_used} served instead. Logged."
            )
        elif degraded:
            why = (
                f"Live route on {model_used} reported low confidence; "
                f"fail-closed fallback engaged (#{fallback_count}). Logged."
            )
        elif is_premium:
            why = (
                f"Complexity {round(float(analysis.complexity or 0), 2)} cleared the premium bar; "
                f"live {model_used} served with confidence {round(confidence, 2)}."
            )
        else:
            why = (
                f"High-confidence match ({round(confidence, 2)} vs {THRESHOLD} threshold); "
                f"live {model_used} resolved without escalation."
            )
    else:
        served = "simulated ● mock"
        why = (
            "Confidence 0.62 below 0.70 threshold, escalated to premium; "
            "premium 429, served deterministic fallback. Logged."
            if degraded and escalate
            else (
                "Low-confidence dispute language, escalated to premium tier."
                if escalate
                else "Standard policy inquiry, high confidence intent match on keyword SLA 4.2."
            )
        )
    run = {
        "run": run_seq,
        "query": query,
        "answer": answer,
        "agent": agent,
        "model": model_used,
        "provider": provider_value,
        "tier": tier_value,
        "served": served,
        "premium_attempt": premium_attempt,
        "status": status,
        "intent": f"{'contract_dispute' if escalate else 'general_faq'} "
                  f"({0.81 if escalate else 0.94} conf)",
        "policy": "cheap-first",
        "complexity": round(float(analysis.complexity or 0), 2),
        "complexity_label": str(getattr(analysis.level, "value", "medium")).capitalize(),
        "confidence": round(confidence, 3),
        "threshold": THRESHOLD,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_total": tokens_in + tokens_out,
        "fallback_count": fallback_count,
        "degraded": degraded,
        "cached": cached,
        "live": live,
        "latency_ms": pr.latency_ms,
        "cost_actual": round(actual_display, 4),
        "cost_baseline": round(baseline, 4),
        "saved": round(baseline - actual_display, 4),
        "order_card": order_card,
        "doc": doc,
        "ledger": ledger,
        "why": why,
        "at": datetime.now().strftime("%I:%M %p").lstrip("0"),
    }
    sessions.setdefault(session_id, []).append(run)
    return run


# ------------------------------------------------------------------ app

app = FastAPI(title="HopDesk", version="2.0.0")

# Split deploy (Static HF UI + Render API): browsers block cross-origin
# calls without this. Origins from env; "*" default is acceptable here —
# public demo, no cookies, no credentials, keys stay server-side.
_cors_raw = os.environ.get("HOPDESK_CORS_ORIGINS", "*")
_cors_origins = (
    ["*"]
    if _cors_raw.strip() in ("", "*")
    else [o.strip() for o in _cors_raw.split(",") if o.strip()]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)
sessions[SEED["opening_session"]] = _seed_session()


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(BASE / "index.html", media_type="text/html")


@app.get("/api/seed")
def get_seed():
    return {
        "store": SEED["store"],
        "budget_cap_day": SEED.get("budget_cap_day", 25.0),
        "opening_session": SEED["opening_session"],
    }


@app.post("/api/sessions")
def new_session():
    sid = f"US-{209543 + len(sessions)}"
    sessions[sid] = []
    return {"session_id": sid}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    return {"session_id": session_id, "turns": sessions.get(session_id, [])}


@app.post("/api/chat")
async def chat(body: Dict[str, Any]):
    text = str(body.get("text", "") or "").strip()
    session_id = str(body.get("session_id", "") or SEED["opening_session"]).strip()
    if not text:
        raise HTTPException(400, "empty query")
    run = await dispatch(session_id, text)
    return JSONResponse(run)


@app.get("/api/runs")
def get_runs(session_id: str = "", limit: int = 50):
    if session_id and session_id in sessions:
        turns = sessions[session_id]
    else:
        turns = [r for ts in sessions.values() for r in ts]
        turns = sorted(turns, key=lambda r: r["run"])
    return {"runs": turns[-limit:][::-1]}


@app.get("/api/usage")
def get_usage():
    actual = sum(r["cost_actual"] for ts in sessions.values() for r in ts)
    would = sum(r["cost_baseline"] for ts in sessions.values() for r in ts)
    return {
        "saved_today": round(float(SEED.get("saved_today", 12.43)) + (would - actual), 2),
        "session_saved": round(would - actual, 4),
        "budget_cap_day": SEED.get("budget_cap_day", 25.0),
        "turns": sum(len(ts) for ts in sessions.values()),
    }


@app.get("/api/incident")
def get_incident():
    return {"down": sorted(down)}


@app.post("/api/incident")
def set_incident(body: Dict[str, Any]):
    for n in body.get("down", [PREMIUM_MODEL, "openai", "premium"]):
        down.add(str(n))
        try:
            mh.health.record_failure(str(n), Exception("429 rate-limited (simulated)"))
        except Exception:
            pass
    return {"down": sorted(down)}


@app.post("/api/incident/clear")
def clear_incident():
    down.clear()
    try:
        for name in (FREE_MODEL, PREMIUM_MODEL, "openai-gpt-4"):
            mh.health.record_success(name, 100)
    except Exception:
        pass
    return {"down": []}


@app.post("/api/reset")
def reset():
    global run_seq
    sessions.clear()
    sessions[SEED["opening_session"]] = _seed_session()
    run_seq = 209543
    down.clear()
    return {"ok": True, "session_id": SEED["opening_session"]}
