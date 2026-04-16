import json
import os
import boto3
from botocore.config import Config
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)

REGION           = os.getenv("REGION", "ap-northeast-2")
LAMBDA_FUNCTION  = os.getenv("LAMBDA_FUNCTION", "bedrock-qa")
RUNTIME_ARN      = os.getenv("AGENTCORE_RUNTIME_ARN", "")

_lambda = boto3.client(
    "lambda",
    region_name=REGION,
    config=Config(read_timeout=900, connect_timeout=10),
)
_agentcore = boto3.client(
    "bedrock-agentcore",
    region_name=REGION,
    config=Config(read_timeout=900, connect_timeout=10),
)


def _invoke_lambda(method: str, path: str, body=None, qs: dict = None):
    event = {
        "httpMethod": method,
        "path": path,
        "queryStringParameters": qs or {},
        "body": json.dumps(body, ensure_ascii=False) if body is not None else None,
    }
    resp = _lambda.invoke(
        FunctionName=LAMBDA_FUNCTION,
        InvocationType="RequestResponse",
        Payload=json.dumps(event).encode(),
    )
    return json.loads(resp["Payload"].read())


def _invoke_agentcore(session_id: str, payload: dict) -> dict:
    resp = _agentcore.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        runtimeSessionId=session_id or "default",
        payload=json.dumps(payload).encode(),
    )
    body = resp.get("body")
    if hasattr(body, "read"):
        body = body.read()
    return json.loads(body) if body else {}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/ask")
async def ask(request: Request):
    body = await request.json()
    session_id = body.get("session_id", "")

    # Route to AgentCore Runtime if configured, otherwise fall back to Lambda
    if RUNTIME_ARN:
        result = _invoke_agentcore(session_id, body)
        return Response(
            content=json.dumps(result, ensure_ascii=False),
            status_code=200,
            media_type="application/json",
        )

    # Lambda fallback (used before AgentCore is provisioned)
    result = _invoke_lambda("POST", "/ask", body)
    return Response(
        content=result.get("body", "{}"),
        status_code=result.get("statusCode", 200),
        media_type="application/json",
    )


@app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
async def proxy(path: str, request: Request):
    body = None
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = None

    result = _invoke_lambda(request.method, "/" + path, body, dict(request.query_params))
    content_type = result.get("headers", {}).get("Content-Type", "application/json")
    return Response(
        content=result.get("body", "{}"),
        status_code=result.get("statusCode", 200),
        media_type=content_type,
    )
