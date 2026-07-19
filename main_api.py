""" from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
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
async def generate(request: AIRequest):
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
"""  """ """