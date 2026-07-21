# ── Stage 1: deps ────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim

# Security: run as non-root
#RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser
RUN addgroup --system --gid 1000 appgroup && adduser --system --uid 1000 --ingroup appgroup appuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application
COPY ai_microservice_guardrails_redis_observability.py .

# Optional: copy your guardrail / observability repos if available locally
# COPY aiguardrail-pipeline /root/aiguardrail-pipeline
# COPY aillm-observability  /root/aillm-observability

USER appuser

EXPOSE 8000

# Health check — Docker and ECS both use this
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["python", "-m", "uvicorn", \
     "ai_microservice_guardrails_redis_observability:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--log-level", "info"]
