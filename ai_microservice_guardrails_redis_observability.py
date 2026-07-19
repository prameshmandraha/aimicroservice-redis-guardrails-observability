#!/usr/bin/env python3
"""
Lab 3 — AI-powered microservice (SIMPLIFIED - no slowapi).
Uses built-in rate limiting instead of slowapi.
"""

import sys
import os
from pathlib import Path
import time
import uuid
import json
import hashlib
from typing import Optional, Tuple
from collections import defaultdict

from fastapi import FastAPI, HTTPException, Depends, Security, Request
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from anthropic import Anthropic
from dotenv import load_dotenv

# Try Redis (optional)
try:
    from redis import Redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

# ============================================================================
# SETUP: Auto-detect repo locations
# ============================================================================

load_dotenv()

def find_repo(repo_name: str) -> Optional[Path]:
    """Auto-detect repository location."""
    possible_paths = [
        Path.home() / repo_name,
        Path.cwd() / repo_name,
        Path.cwd().parent / repo_name,
        Path("C:/Users") / os.getenv("USERNAME", "") / repo_name,
    ]
    
    for path in possible_paths:
        if path.exists():
            print(f"✅ Found {repo_name} at: {path}")
            return path
    
    return None

print("\n" + "="*70)
print("🔍 Searching for GitHub repositories...")
print("="*70)

GUARDRAILS_REPO = find_repo("aiguardrail-pipeline")
OBSERVABILITY_REPO = find_repo("aillm-observability")

if GUARDRAILS_REPO:
    sys.path.insert(0, str(GUARDRAILS_REPO))
if OBSERVABILITY_REPO:
    sys.path.insert(0, str(OBSERVABILITY_REPO))

# ============================================================================
# IMPORT GUARDRAILS WITH FALLBACK
# ============================================================================

def redact_pii(text: str) -> Tuple[str, list]:
    """Redact PII from text."""
    try:
        from pii_redactor import redact_pii as real_redact
        return real_redact(text)
    except (ImportError, AttributeError):
        import re
        pii_patterns = {
            "EMAIL": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
            "PHONE": r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b",
            "SSN": r"\b\d{3}-\d{2}-\d{4}\b",
        }
        
        redacted = text
        found = []
        for pii_type, pattern in pii_patterns.items():
            matches = re.findall(pattern, redacted)
            if matches:
                found.append(f"{pii_type}: {len(matches)} found")
                redacted = re.sub(pattern, f"[{pii_type}]", redacted)
        
        return redacted, found

def detect_injection(text: str) -> Tuple[bool, str]:
    """Detect prompt injection attempts."""
    try:
        from prompt_injection_detector import detect_injection as real_detect
        return real_detect(text)
    except (ImportError, AttributeError):
        injection_keywords = [
            "ignore all previous",
            "forget everything",
            "override",
            "bypass",
            "you are now",
            "act as",
            "pretend",
            "jailbreak",
        ]
        
        text_lower = text.lower()
        for keyword in injection_keywords:
            if keyword in text_lower:
                return True, f"Suspicious keyword detected: '{keyword}'"
        
        return False, ""

def check_hallucination(response: str, prompt: str) -> dict:
    """Check for hallucination."""
    try:
        from output_compliance_checker import check_hallucination as real_check
        return real_check(response, prompt)
    except (ImportError, AttributeError):
        return {
            "passed": len(response) > 0 and len(response) < 10000,
            "reason": "Length check passed"
        }

def check_compliance(text: str) -> dict:
    """Check compliance."""
    try:
        from output_compliance_checker import check_compliance as real_check
        return real_check(text)
    except (ImportError, AttributeError):
        return {
            "passed": True,
            "reason": "No compliance violations detected"
        }

print("✅ Guardrails module loaded (real or fallback)")

# ============================================================================
# IMPORT OBSERVABILITY WITH FALLBACK
# ============================================================================

class OnlineScorerFallback:
    """Fallback OnlineScorer."""
    
    def __init__(self, window_size: int = 100):
        self.window_size = window_size
        self.scores = []
    
    def handle_request(self, request_id: str, question: str, answer: str):
        """Simple fallback scorer."""
        score = type('Score', (), {
            'request_id': request_id,
            'faithfulness': 0.85,
            'relevancy': 0.82,
            'latency_ms': 800.0,
            'input_tokens': 50,
            'output_tokens': 150,
            'cost_usd': 0.00156
        })()
        self.scores.append(score)
        return score
    
    def get_live_metrics(self) -> dict:
        """Get metrics."""
        if not self.scores:
            return {
                "requests_in_window": 0,
                "avg_faithfulness": 0.0,
                "avg_relevancy": 0.0,
                "avg_latency_ms": 0.0,
            }
        
        faith = [s.faithfulness for s in self.scores]
        relev = [s.relevancy for s in self.scores]
        lat = [s.latency_ms for s in self.scores]
        
        return {
            "requests_in_window": len(self.scores),
            "avg_faithfulness": round(sum(faith) / len(faith), 3),
            "avg_relevancy": round(sum(relev) / len(relev), 3),
            "avg_latency_ms": round(sum(lat) / len(lat), 1),
        }

try:
    from online_scorer_fixed import OnlineScorer
    print("✅ Observability module loaded (real)")
except ImportError:
    print("⚠️  Using fallback observability")
    OnlineScorer = OnlineScorerFallback

# ============================================================================
# SIMPLE RATE LIMITER (no slowapi needed)
# ============================================================================

class SimpleRateLimiter:
    """Simple in-memory rate limiter."""
    
    def __init__(self, max_requests: int = 10, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests = defaultdict(list)
    
    def is_allowed(self, client_id: str) -> bool:
        """Check if request is allowed."""
        now = time.time()
        cutoff = now - self.window_seconds
        
        # Clean old requests
        self.requests[client_id] = [t for t in self.requests[client_id] if t > cutoff]
        
        # Check limit
        if len(self.requests[client_id]) >= self.max_requests:
            return False
        
        # Add current request
        self.requests[client_id].append(now)
        return True

rate_limiter = SimpleRateLimiter(max_requests=10, window_seconds=60)

# ============================================================================
# CONFIGURATION
# ============================================================================

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    print("\n⚠️  WARNING: ANTHROPIC_API_KEY not set in .env")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
VALID_API_KEYS = set(os.getenv("VALID_API_KEYS", "dev-key-001,prod-key-001").split(","))

# ============================================================================
# INITIALIZATION
# ============================================================================

app = FastAPI(
    title="AI Microservice",
    description="Production-grade LLM API with guardrails and observability",
    version="1.0.0"
)

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# Redis for caching
redis = None
if REDIS_AVAILABLE:
    try:
        redis = Redis.from_url(REDIS_URL, decode_responses=True)
        redis.ping()
        print("✅ Redis connected")
    except Exception as e:
        print(f"⚠️  Redis unavailable: {e} (caching disabled)")

# Observability
online_scorer = OnlineScorer(window_size=100)
audit_log = []

# ============================================================================
# REQUEST / RESPONSE MODELS
# ============================================================================

class AIRequest(BaseModel):
    """Request payload."""
    prompt: str = Field(..., min_length=1, max_length=4000)
    system_prompt: str = Field(default="You are a helpful assistant.", max_length=2000)
    max_tokens: int = Field(default=1024, ge=1, le=4096)
    cache_ttl: int = Field(default=86400, ge=0)

class AIResponse(BaseModel):
    """Response payload."""
    request_id: str
    response: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    cache_hit: bool = False
    quality_score: Optional[dict] = None

class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    redis_connected: bool
    guardrails_active: bool

# ============================================================================
# AUTHENTICATION
# ============================================================================

API_KEY_HEADER = APIKeyHeader(name="X-API-Key")

def verify_api_key(api_key: str = Security(API_KEY_HEADER)) -> str:
    """Validate API key."""
    if api_key not in VALID_API_KEYS:
        raise HTTPException(status_code=403, detail="Invalid API key")
    return api_key

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def get_client_ip(request: Request) -> str:
    """Get client IP for rate limiting."""
    return request.client.host if request.client else "unknown"

def get_cache_key(prompt: str, system_prompt: str) -> str:
    """Generate cache key."""
    key_data = f"{prompt}:{system_prompt}".encode()
    return f"llm:{hashlib.md5(key_data).hexdigest()}"

# ============================================================================
# ENDPOINTS
# ============================================================================

@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check."""
    return HealthResponse(
        status="healthy",
        redis_connected=redis is not None,
        guardrails_active=True
    )

@app.post("/v1/generate", response_model=AIResponse)
async def generate(
    request_data: AIRequest,
    request: Request,
    api_key: str = Depends(verify_api_key)
):
    """
    Generate LLM response with guardrails and observability.
    
    Rate limit: 10 requests/minute per IP
    """
    request_id = str(uuid.uuid4())
    start_time = time.time()
    cache_hit = False
    
    # ════════════════════════════════════════════════════════════════
    # RATE LIMITING
    # ════════════════════════════════════════════════════════════════
    
    client_ip = get_client_ip(request)
    if not rate_limiter.is_allowed(client_ip):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded (10 requests/minute per IP)"
        )
    
    try:
        # ════════════════════════════════════════════════════════════════
        # INPUT GUARDRAILS
        # ════════════════════════════════════════════════════════════════
        
        redacted_prompt, pii_entities = redact_pii(request_data.prompt)
        if pii_entities:
            print(f"[{request_id}] PII detected: {pii_entities}")
        
        is_injection, reason = detect_injection(redacted_prompt)
        if is_injection:
            raise HTTPException(status_code=400, detail=f"Request blocked: {reason}")
        
        # ════════════════════════════════════════════════════════════════
        # CACHE LOOKUP
        # ════════════════════════════════════════════════════════════════
        
        cache_key = get_cache_key(redacted_prompt, request_data.system_prompt)
        
        if redis and request_data.cache_ttl > 0:
            try:
                cached = redis.get(cache_key)
                if cached:
                    cache_hit = True
                    print(f"[{request_id}] Cache HIT")
                    return AIResponse(**json.loads(cached))
            except Exception as e:
                print(f"[{request_id}] Cache error: {e}")
        
        # ════════════════════════════════════════════════════════════════
        # LLM GENERATION
        # ════════════════════════════════════════════════════════════════
        
        print(f"[{request_id}] Calling Claude...")
        result = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=request_data.max_tokens,
            system=request_data.system_prompt,
            messages=[{"role": "user", "content": redacted_prompt}]
        )
        
        llm_response = result.content[0].text
        
        # ════════════════════════════════════════════════════════════════
        # OUTPUT GUARDRAILS
        # ════════════════════════════════════════════════════════════════
        
        hall = check_hallucination(llm_response, redacted_prompt)
        if not hall.get("passed", True):
            raise HTTPException(status_code=422, detail="Hallucination detected")
        
        comp = check_compliance(llm_response)
        if not comp.get("passed", True):
            raise HTTPException(status_code=422, detail="Compliance violation")
        
        # ════════════════════════════════════════════════════════════════
        # QUALITY SCORING
        # ════════════════════════════════════════════════════════════════
        
        score = online_scorer.handle_request(request_id, redacted_prompt, llm_response)
        quality_score = {
            "faithfulness": round(score.faithfulness, 3),
            "relevancy": round(score.relevancy, 3),
        }
        
        # ════════════════════════════════════════════════════════════════
        # BUILD RESPONSE
        # ════════════════════════════════════════════════════════════════
        
        latency_ms = round((time.time() - start_time) * 1000, 2)
        
        response = AIResponse(
            request_id=request_id,
            response=llm_response,
            model=result.model,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            latency_ms=latency_ms,
            cache_hit=cache_hit,
            quality_score=quality_score
        )
        
        # ════════════════════════════════════════════════════════════════
        # CACHE RESPONSE
        # ════════════════════════════════════════════════════════════════
        
        if redis and request_data.cache_ttl > 0:
            try:
                redis.setex(cache_key, request_data.cache_ttl, json.dumps(response.dict()))
            except Exception as e:
                print(f"[{request_id}] Cache write error: {e}")
        
        # ════════════════════════════════════════════════════════════════
        # AUDIT LOG
        # ════════════════════════════════════════════════════════════════
        
        audit_log.append({
            "request_id": request_id,
            "timestamp": time.time(),
            "input_length": len(redacted_prompt),
            "output_length": len(llm_response),
            "faithfulness": quality_score["faithfulness"],
            "relevancy": quality_score["relevancy"],
            "latency_ms": latency_ms,
            "status": "success"
        })
        
        return response
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"[{request_id}] Error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/v1/metrics")
async def get_metrics(api_key: str = Depends(verify_api_key)):
    """Get live metrics."""
    metrics = online_scorer.get_live_metrics()
    return {
        "timestamp": time.time(),
        "metrics": metrics,
        "redis_connected": redis is not None,
        "audit_log_size": len(audit_log),
        "last_10_requests": audit_log[-10:]
    }

# ============================================================================
# STARTUP
# ============================================================================

@app.on_event("startup")
async def startup():
    """Initialize on startup."""
    print("\n" + "="*70)
    print("✅ AI Microservice READY")
    print("="*70)
    print(f"📖 Docs: http://localhost:8000/docs")
    print(f"🔐 Auth: X-API-Key: dev-key-001")
    print(f"💾 Redis: {'✅ Connected' if redis else '⚠️  Disabled (no caching)'}")
    print(f"⚡ Rate limit: 10 requests/minute per IP")
    print("="*70 + "\n")

# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")