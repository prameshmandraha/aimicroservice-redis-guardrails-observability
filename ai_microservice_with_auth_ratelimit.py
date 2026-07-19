from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from fastapi import Depends, Security
from fastapi.security import APIKeyHeader
from slowapi import Limiter
from slowapi.util import get_remote_address
from anthropic import Anthropic
import uuid, time
from dotenv import load_dotenv
import os

load_dotenv()

# Ensure the ANTHROPIC_API_KEY from the .env is loaded into the environment
if not os.getenv("ANTHROPIC_API_KEY"):
    raise RuntimeError("ANTHROPIC_API_KEY not set in environment. Ensure .env contains ANTHROPIC_API_KEY")


app = FastAPI(title="AI Service", version="1.0.0")
client = Anthropic()

API_KEY_HEADER = APIKeyHeader(name="X-API-Key")
VALID_KEYS = {"dev-key-001"}  # Load from env in prod

def verify_api_key(api_key: str = Security(API_KEY_HEADER)):
    if api_key not in VALID_KEYS:
        raise HTTPException(status_code=403, detail="Invalid API key")
    return api_key

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter

class AIRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000)
    system_prompt: str = Field(default="You are a helpful assistant.")
    max_tokens: int = Field(default=1024, ge=1, le=4096)

class AIResponse(BaseModel):
    request_id: str
    response: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float

@app.post("/v1/generate", response_model=AIResponse)
@limiter.limit("10/minute")
async def generate(request: AIRequest,api_key: str = Depends(verify_api_key)):
    request_id = str(uuid.uuid4())
    start = time.time()
    
    result = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=request.max_tokens,
        system=request.system_prompt,
        messages=[{"role": "user", "content": request.prompt}]
    )
    
    return AIResponse(
        request_id=request_id,
        response=result.content[0].text,
        model=result.model,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        latency_ms=round((time.time() - start) * 1000, 2)
    )

@app.get("/health")
async def health():
    return {"status": "ok"}
