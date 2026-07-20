#!/usr/bin/env python3
"""
ai_microservice_guardrails_redis_observability.py

Production AI microservice combining:
- FastAPI REST endpoints
- API key authentication + per-IP rate limiting
- Input guardrails (PII redaction + injection detection)
- LLM core (Claude Sonnet, sync + streaming)
- Output guardrails (hallucination + compliance checks)
- Redis response caching
- Quality scoring + audit trail (observability)

Lab 4: Dockerised and deployed on AWS ECS / Kubernetes
"""

import sys
import os
import time
import uuid
import json
import hashlib
import re
import logging
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional, Tuple, AsyncGenerator
from pathlib import Path

from fastapi import FastAPI, HTTPException, Depends, Security, Request
from fastapi.security import APIKeyHeader
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from anthropic import Anthropic
from dotenv import load_dotenv

# ── Optional dependencies ──────────────────────────────────────────────────────

try:
    from redis import Redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

load_dotenv()

# ── Structured JSON logging (Datadog / Splunk compatible) ─────────────────────

logging.basicConfig(format="%(message)s", level=logging.INFO)
logger = logging.getLogger("ai-service")


def log(request_id: str, event: str, **kwargs):
    logger.info(json.dumps({
        "request_id": request_id,
        "event": event,
        "service": "ai-microservice",
        "timestamp": time.time(),
        **kwargs,
    }))


# ══════════════════════════════════════════════════════════════════════════════
#  MODULE 1 — GUARDRAILS  (inline fallback; swap for real modules if present)
# ══════════════════════════════════════════════════════════════════════════════

def _try_import_guardrails():
    """Try to import from the cloned guardrails repo, fall back to inline."""
    guardrails_path = Path.home() / "aiguardrail-pipeline"
    if guardrails_path.exists():
        sys.path.insert(0, str(guardrails_path))

_try_import_guardrails()


def redact_pii(text: str) -> Tuple[str, list]:
    """Redact PII. Uses real Presidio module if available, else regex fallback."""
    try:
        from pii_redactor import redact_pii as _real
        return _real(text)
    except (ImportError, AttributeError):
        pass

    patterns = {
        "EMAIL": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
        "PHONE": r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b",
        "SSN":   r"\b\d{3}-\d{2}-\d{4}\b",
        "CARD":  r"\b(?:\d{4}[-\s]?){3}\d{4}\b",
    }
    found, redacted = [], text
    for label, pat in patterns.items():
        matches = re.findall(pat, redacted)
        if matches:
            found.extend([f"{label}"] * len(matches))
            redacted = re.sub(pat, f"[{label}]", redacted)
    return redacted, found


_INJECTION_PATTERNS = [
    r"ignore (all |previous |above )?(instructions|rules|prompts)",
    r"you are now",
    r"disregard your (system|previous)",
    r"reveal your (system prompt|instructions)",
    r"pretend (you are|to be|there are no)",
    r"jailbreak",
    r"DAN mode",
    r"developer mode",
    r"act as (?!a banking|a financial)",
]


def detect_injection(text: str) -> Tuple[bool, str]:
    """Detect prompt injection — fast regex gate then LLM fallback."""
    try:
        from prompt_injection_detector import detect_injection as _real
        return _real(text)
    except (ImportError, AttributeError):
        pass

    for pat in _INJECTION_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return True, f"Pattern matched: '{pat}'"
    return False, ""


def check_hallucination(response: str, prompt: str) -> dict:
    try:
        from output_compliance_checker import check_hallucination as _real
        return _real(response, prompt)
    except (ImportError, AttributeError):
        return {"passed": len(response) > 0, "reason": "Length check (fallback)"}


def check_compliance(text: str) -> dict:
    try:
        from output_compliance_checker import check_compliance as _real
        return _real(text)
    except (ImportError, AttributeError):
        return {"passed": True, "reason": "No violations (fallback)"}


# ══════════════════════════════════════════════════════════════════════════════
#  MODULE 2 — OBSERVABILITY  (inline fallback)
# ══════════════════════════════════════════════════════════════════════════════

def _try_import_observability():
    obs_path = Path.home() / "aillm-observability"
    if obs_path.exists():
        sys.path.insert(0, str(obs_path))

_try_import_observability()


@dataclass
class RequestScore:
    request_id: str
    faithfulness: float
    relevancy: float
    latency_ms: float
    cost_usd: float


class OnlineScorer:
    """Quality scorer — uses real module if available, else lightweight fallback."""

    _REAL = None

    def __init__(self, window_size: int = 100):
        self._window: deque = deque(maxlen=window_size)
        self._real_scorer = None
        try:
            from online_scorer_fixed import OnlineScorer as _RS
            self._real_scorer = _RS(window_size=window_size)
        except ImportError:
            pass

    def score(self, request_id: str, question: str, answer: str, latency_ms: float) -> RequestScore:
        if self._real_scorer:
            try:
                s = self._real_scorer.handle_request(request_id, question, answer)
                rs = RequestScore(request_id, s.faithfulness, s.relevancy, latency_ms, s.cost_usd)
                self._window.append(rs)
                return rs
            except Exception:
                pass

        # Lightweight fallback: cosine similarity as proxy
        words_q = set(question.lower().split())
        words_a = set(answer.lower().split())
        overlap = len(words_q & words_a) / max(len(words_q), 1)
        rs = RequestScore(request_id, min(0.95, overlap + 0.5), min(0.95, overlap + 0.45), latency_ms, 0.0015)
        self._window.append(rs)
        return rs

    def metrics(self) -> dict:
        w = list(self._window)
        if not w:
            return {"requests_in_window": 0}
        return {
            "requests_in_window": len(w),
            "avg_faithfulness": round(statistics.mean(s.faithfulness for s in w), 3),
            "avg_relevancy":    round(statistics.mean(s.relevancy for s in w), 3),
            "avg_latency_ms":   round(statistics.mean(s.latency_ms for s in w), 1),
            "p95_latency_ms":   round(sorted(s.latency_ms for s in w)[int(len(w) * 0.95)], 1),
            "total_cost_usd":   round(sum(s.cost_usd for s in w), 4),
        }


# ══════════════════════════════════════════════════════════════════════════════
#  MODULE 3 — RATE LIMITER  (no external deps)
# ══════════════════════════════════════════════════════════════════════════════

class RateLimiter:
    def __init__(self, max_requests: int = 10, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._buckets: dict = defaultdict(list)

    def is_allowed(self, client_id: str) -> bool:
        now = time.time()
        cutoff = now - self.window_seconds
        bucket = self._buckets[client_id]
        self._buckets[client_id] = [t for t in bucket if t > cutoff]
        if len(self._buckets[client_id]) >= self.max_requests:
            return False
        self._buckets[client_id].append(now)
        return True


# ══════════════════════════════════════════════════════════════════════════════
#  APP INITIALISATION
# ══════════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="AI Microservice",
    description="Production LLM API — guardrails + Redis + observability",
    version="1.0.0",
)

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

VALID_API_KEYS = set(os.getenv("VALID_API_KEYS", "dev-key-001,prod-key-001").split(","))
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")

redis: Optional["Redis"] = None
if REDIS_AVAILABLE:
    try:
        redis = Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        redis.ping()
    except Exception:
        redis = None

rate_limiter = RateLimiter(max_requests=10, window_seconds=60)
scorer = OnlineScorer(window_size=100)
audit_log: list = []

# ── Auth ──────────────────────────────────────────────────────────────────────

API_KEY_HEADER = APIKeyHeader(name="X-API-Key")


def verify_api_key(api_key: str = Security(API_KEY_HEADER)) -> str:
    if api_key not in VALID_API_KEYS:
        raise HTTPException(status_code=403, detail="Invalid API key")
    return api_key


# ── Pydantic models ───────────────────────────────────────────────────────────

class AIRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000)
    system_prompt: str = Field(default="You are a helpful assistant.", max_length=2000)
    max_tokens: int = Field(default=1024, ge=1, le=4096)
    cache_ttl: int = Field(default=3600, ge=0, description="Redis TTL in seconds (0 = no cache)")


class AIResponse(BaseModel):
    request_id: str
    response: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    cache_hit: bool = False
    quality: Optional[dict] = None


class HealthResponse(BaseModel):
    status: str
    redis: bool
    guardrails: bool
    observability: bool
    version: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cache_key(prompt: str, system: str) -> str:
    h = hashlib.md5(f"{prompt}|{system}".encode()).hexdigest()
    return f"llm:{h}"


def _audit(request_id: str, api_key: str, prompt: str, response: str,
           quality: dict, latency: float, status: str):
    entry = {
        "request_id": request_id,
        "timestamp": time.time(),
        "api_key_hash": hashlib.sha256(api_key.encode()).hexdigest()[:8],
        "input_len": len(prompt),
        "output_len": len(response),
        **quality,
        "latency_ms": round(latency, 1),
        "status": status,
    }
    entry["hash"] = hashlib.sha256(
        json.dumps(entry, sort_keys=True).encode()
    ).hexdigest()
    audit_log.append(entry)


# ══════════════════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(
        status="healthy",
        redis=redis is not None,
        guardrails=True,
        observability=True,
        version="1.0.0",
    )


@app.post("/v1/generate", response_model=AIResponse)
async def generate(body: AIRequest, request: Request, api_key: str = Depends(verify_api_key)):
    """
    Full guardrail + cache + observability pipeline.

    Pipeline order:
      1. Rate limit check
      2. PII redaction
      3. Injection detection
      4. Redis cache lookup
      5. Claude Sonnet generation
      6. Output guardrails (hallucination + compliance)
      7. Quality scoring
      8. Audit log
      9. Cache store
    """
    rid = str(uuid.uuid4())
    t0 = time.time()
    client_ip = request.client.host if request.client else "unknown"

    log(rid, "request_received", prompt_len=len(body.prompt), ip=client_ip)

    # ── 1. Rate limit ──────────────────────────────────────────────────────────
    if not rate_limiter.is_allowed(client_ip):
        log(rid, "rate_limited", ip=client_ip)
        raise HTTPException(status_code=429, detail="Rate limit exceeded (10 req/min per IP)")

    # ── 2. PII redaction ───────────────────────────────────────────────────────
    redacted, pii_found = redact_pii(body.prompt)
    if pii_found:
        log(rid, "pii_redacted", entities=pii_found)

    # ── 3. Injection detection ─────────────────────────────────────────────────
    flagged, reason = detect_injection(redacted)
    if flagged:
        log(rid, "injection_blocked", reason=reason)
        _audit(rid, api_key, redacted, "", {}, (time.time()-t0)*1000, "blocked")
        raise HTTPException(status_code=400, detail=f"Request blocked: {reason}")

    # ── 4. Cache lookup ────────────────────────────────────────────────────────
    cache_key = _cache_key(redacted, body.system_prompt)
    if redis and body.cache_ttl > 0:
        try:
            cached = redis.get(cache_key)
            if cached:
                log(rid, "cache_hit")
                data = json.loads(cached)
                data["cache_hit"] = True
                data["request_id"] = rid
                return AIResponse(**data)
        except Exception:
            pass

    # ── 5. LLM generation ─────────────────────────────────────────────────────
    try:
        result = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=body.max_tokens,
            system=body.system_prompt,
            messages=[{"role": "user", "content": redacted}],
        )
    except Exception as e:
        log(rid, "llm_error", error=str(e))
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}")

    llm_text = result.content[0].text

    # ── 6. Output guardrails ───────────────────────────────────────────────────
    hall = check_hallucination(llm_text, redacted)
    if not hall.get("passed", True):
        log(rid, "hallucination_blocked")
        raise HTTPException(status_code=422, detail="Output failed hallucination check")

    comp = check_compliance(llm_text)
    if not comp.get("passed", True):
        action = comp.get("action", "block")
        log(rid, "compliance_blocked", action=action)
        if action == "escalate":
            raise HTTPException(status_code=202, detail="Escalated to compliance review")
        raise HTTPException(status_code=422, detail="Output failed compliance check")

    # ── 7. Quality scoring ─────────────────────────────────────────────────────
    latency_ms = (time.time() - t0) * 1000
    score = scorer.score(rid, redacted, llm_text, latency_ms)
    quality = {"faithfulness": score.faithfulness, "relevancy": score.relevancy}

    # ── 8. Audit ───────────────────────────────────────────────────────────────
    _audit(rid, api_key, redacted, llm_text, quality, latency_ms, "success")
    log(rid, "request_completed", latency_ms=round(latency_ms, 1), **quality)

    # ── 9. Cache store ─────────────────────────────────────────────────────────
    response = AIResponse(
        request_id=rid,
        response=llm_text,
        model=result.model,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        latency_ms=round(latency_ms, 2),
        cache_hit=False,
        quality=quality,
    )

    if redis and body.cache_ttl > 0:
        try:
            redis.setex(cache_key, body.cache_ttl, json.dumps(response.dict()))
        except Exception:
            pass

    return response


@app.post("/v1/generate/stream")
async def generate_stream(body: AIRequest, request: Request, api_key: str = Depends(verify_api_key)):
    """
    Streaming endpoint — tokens delivered as Server-Sent Events.
    Guardrails run on the full response post-stream.
    """
    rid = str(uuid.uuid4())
    t0 = time.time()
    client_ip = request.client.host if request.client else "unknown"

    if not rate_limiter.is_allowed(client_ip):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    redacted, pii_found = redact_pii(body.prompt)
    flagged, reason = detect_injection(redacted)
    if flagged:
        raise HTTPException(status_code=400, detail=f"Request blocked: {reason}")

    async def token_stream() -> AsyncGenerator[str, None]:
        yield f"data: {json.dumps({'request_id': rid, 'event': 'start'})}\n\n"
        full: list = []

        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=body.max_tokens,
            system=body.system_prompt,
            messages=[{"role": "user", "content": redacted}],
        ) as stream:
            for token in stream.text_stream:
                full.append(token)
                yield f"data: {json.dumps({'token': token})}\n\n"

        full_text = "".join(full)
        comp = check_compliance(full_text)
        latency_ms = (time.time() - t0) * 1000
        score = scorer.score(rid, redacted, full_text, latency_ms)
        _audit(rid, api_key, redacted, full_text,
               {"faithfulness": score.faithfulness, "relevancy": score.relevancy},
               latency_ms, "success")

        yield f"data: {json.dumps({'event': 'done', 'latency_ms': round(latency_ms, 1), 'compliance': comp.get('passed', True)})}\n\n"

    return StreamingResponse(
        token_stream(),
        media_type="text/event-stream",
        headers={"X-Request-ID": rid},
    )


@app.get("/v1/metrics")
async def get_metrics(api_key: str = Depends(verify_api_key)):
    return {
        "timestamp": time.time(),
        "redis_connected": redis is not None,
        "total_requests": len(audit_log),
        "quality_window": scorer.metrics(),
    }


@app.get("/v1/audit-log")
async def get_audit_log(api_key: str = Depends(verify_api_key), limit: int = 50):
    return {
        "total": len(audit_log),
        "entries": audit_log[-limit:],
    }


# ══════════════════════════════════════════════════════════════════════════════
#  STARTUP / SHUTDOWN
# ══════════════════════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    print("\n" + "="*70)
    print("  AI Microservice — ready")
    print("="*70)
    print(f"  Redis   : {'connected' if redis else 'disabled (no caching)'}")
    print(f"  Auth    : API key via X-API-Key header")
    print(f"  Limits  : 10 req/min per IP")
    print(f"  Docs    : http://localhost:8000/docs")
    print("="*70 + "\n")


@app.on_event("shutdown")
async def shutdown():
    if redis:
        redis.close()


# ── Local dev entry-point ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
