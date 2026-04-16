import json
import os
import uuid

from config import API_URL, MODEL_ID
from session import _load_history, _save_history, session_handler, history_handler, compact_handler, delete_handler
from agent import ask_with_tools
from memory import extract_and_store
from webhook import handle_webhook

# Load UI from separate file so Python string escaping never corrupts the HTML/JS
_ui_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")
HTML = open(_ui_path).read().replace("API_URL_PLACEHOLDER", API_URL)


def lambda_handler(event, context):
    # Async webhook processing (invoked by handle_webhook via Lambda:InvokeFunction Event)
    if event.get("_async_webhook"):
        handle_webhook(event.get("payload", {}), async_mode=True)
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

    if method == "GET" and path.endswith("/history"):
        return history_handler()

    if method == "GET" and "/session/" in path:
        session_id = path.split("/session/")[-1].strip("/")
        return session_handler(session_id)

    if method == "DELETE" and "/session/" in path:
        session_id = path.split("/session/")[-1].strip("/")
        return delete_handler(session_id)

    if method == "POST" and "/compact/" in path:
        session_id = path.split("/compact/")[-1].strip("/")
        return compact_handler(session_id)

    try:
        body = json.loads(event.get("body") or "{}")
        question = body.get("question", "").strip()
        if not question:
            return api_response(400, {"error": "question is required"})

        session_id = body.get("session_id", "").strip() or str(uuid.uuid4())
        model = body.get("model", "").strip() or None
        history = _load_history(session_id)

        answer = ask_with_tools(question, history, model=model)

        history.append({"role": "user",      "content": [{"text": question}]})
        history.append({"role": "assistant", "content": [{"text": answer}]})
        _save_history(session_id, history)
        extract_and_store(question, answer)

        return api_response(200, {"answer": answer, "model": MODEL_ID, "session_id": session_id})

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
