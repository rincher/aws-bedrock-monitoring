import json
import os
import re
import uuid

from config import API_URL, MODEL_ID
from agent import MODEL_SONNET

_MYSQL_PATTERN = re.compile(
    r'\b(mysql|sql|databases?|db|tables?|schemas?|query|queries|select|insert|update|delete|'
    r'join|index|rds|aurora|innodb|varchar|columns?|rows?|records?|stored.?proc|trigger|views?|'
    r'clothing_store|show tables|describe|explain|primary key|foreign key)\b',
    re.IGNORECASE,
)

def _resolve_model(question: str, requested: str | None) -> str | None:
    """DB questions always use Sonnet regardless of user selection. Other questions use requested model."""
    if _MYSQL_PATTERN.search(question):
        return MODEL_SONNET
    return requested or None
from session import (
    _load_history, _save_history, session_handler, history_handler, compact_handler, delete_handler,
    complete_async_request, fail_async_request, get_async_request,
)
from agent import ask_with_tools
from memory import extract_and_store
from webhook import handle_webhook

# Load UI from separate file so Python string escaping never corrupts the HTML/JS
_ui_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")
HTML = open(_ui_path).read().replace("API_URL_PLACEHOLDER", API_URL)


def _process_async_ask(payload: dict) -> None:
    """Runs inside the async self-invocation to process a question and store the result."""
    request_id = payload["request_id"]
    question   = payload["question"]
    session_id = payload["session_id"]
    user_id    = payload.get("user_id", "")
    model      = payload.get("model") or None
    try:
        history = _load_history(session_id)
        answer, used_model = ask_with_tools(question, history, model=model)
        history.append({"role": "user",      "content": [{"text": question}]})
        history.append({"role": "assistant", "content": [{"text": answer}]})
        _save_history(session_id, history, user_id=user_id)
        extract_and_store(question, answer)
        complete_async_request(request_id, answer, used_model)
    except Exception as e:
        fail_async_request(request_id, str(e))


def lambda_handler(event, context):
    # Async webhook processing (invoked by handle_webhook via Lambda:InvokeFunction Event)
    if event.get("_async_webhook"):
        handle_webhook(event.get("payload", {}), async_mode=True)
        return

    # Async ask processing (self-invoked via Lambda:InvokeFunction Event)
    if event.get("_async_ask"):
        _process_async_ask(event["_async_ask"])
        return

    method = event.get("requestContext", {}).get("http", {}).get("method") \
             or event.get("httpMethod", "POST")
    path = event.get("requestContext", {}).get("http", {}).get("path") \
           or event.get("path", "/ask")

    if method == "POST" and path.endswith("/webhook"):
        try:
            body = json.loads(event.get("body") or "{}")
            return api_response(200, handle_webhook(body, async_mode=False))
        except Exception as e:
            return api_response(500, {"error": str(e)})

    if method == "GET" and path.endswith("/ui"):
        return {"statusCode": 200, "headers": {"Content-Type": "text/html"}, "body": HTML}

    qs = event.get("queryStringParameters") or {}

    if method == "GET" and path.endswith("/history"):
        return history_handler(user_id=qs.get("user", ""))

    if method == "GET" and "/session/" in path:
        session_id = path.split("/session/")[-1].strip("/")
        return session_handler(session_id)

    if method == "DELETE" and "/session/" in path:
        session_id = path.split("/session/")[-1].strip("/")
        return delete_handler(session_id, user_id=qs.get("user", ""))

    if method == "POST" and "/compact/" in path:
        session_id = path.split("/compact/")[-1].strip("/")
        return compact_handler(session_id, user_id=qs.get("user", ""))

    if method == "GET" and "/result/" in path:
        request_id = path.split("/result/")[-1].strip("/")
        item = get_async_request(request_id)
        if not item:
            return api_response(404, {"error": "Not found"})
        return api_response(200, {
            "status":     item.get("status", "pending"),
            "answer":     item.get("answer", ""),
            "model":      item.get("model", ""),
            "error":      item.get("error", ""),
            "session_id": item.get("req_session_id", ""),
        })

    try:
        body = json.loads(event.get("body") or "{}")
        question = body.get("question", "").strip()
        if not question:
            return api_response(400, {"error": "question is required"})

        session_id = body.get("session_id", "").strip() or str(uuid.uuid4())
        user_id    = body.get("user_id", "").strip()
        model      = _resolve_model(question, body.get("model", "").strip() or None)
        history    = _load_history(session_id)

        answer, used_model = ask_with_tools(question, history, model=model)

        history.append({"role": "user",      "content": [{"text": question}]})
        history.append({"role": "assistant", "content": [{"text": answer}]})
        _save_history(session_id, history, user_id=user_id)
        extract_and_store(question, answer)

        return api_response(200, {"answer": answer, "model": used_model, "session_id": session_id})

    except Exception as e:
        return api_response(500, {"error": str(e)})


def api_response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
        "body": json.dumps(body, ensure_ascii=False),
    }
