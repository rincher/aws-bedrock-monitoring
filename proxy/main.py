import json
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

_lambda = boto3.client(
    "lambda",
    region_name="ap-northeast-2",
    config=Config(read_timeout=900, connect_timeout=10),
)
FUNCTION = "bedrock-qa"


def _invoke(method: str, path: str, body=None, qs: dict = None):
    event = {
        "httpMethod": method,
        "path": path,
        "queryStringParameters": qs or {},
        "body": json.dumps(body, ensure_ascii=False) if body is not None else None,
    }
    resp = _lambda.invoke(
        FunctionName=FUNCTION,
        InvocationType="RequestResponse",
        Payload=json.dumps(event).encode(),
    )
    return json.loads(resp["Payload"].read())


@app.get("/health")
def health():
    return {"status": "ok"}


@app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
async def proxy(path: str, request: Request):
    body = None
    if request.method in ("POST",):
        try:
            body = await request.json()
        except Exception:
            body = None

    result = _invoke(request.method, "/" + path, body, dict(request.query_params))

    content_type = result.get("headers", {}).get("Content-Type", "application/json")
    return Response(
        content=result.get("body", "{}"),
        status_code=result.get("statusCode", 200),
        media_type=content_type,
    )
