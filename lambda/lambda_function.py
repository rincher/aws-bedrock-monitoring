"""
Lambda router.
Since the AgentCore Runtime now handles /ask (via the EC2 sidecar), this Lambda
only serves:
  - GET  /ui                   → chat HTML for the Grafana iframe
  - GET  /history              → session list for the sidebar
  - GET  /session/{id}         → load a specific session
  - DELETE /session/{id}       → delete a session
  - POST /compact/{id}         → compact a long session
  - POST /webhook              → Grafana alert analysis (uses Bedrock via agent.py)
"""
import json
import os

from config import API_URL
from session import session_handler, history_handler, compact_handler, delete_handler
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
           or event.get("path", "/")

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

    return api_response(404, {"error": "Not found"})


def api_response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
        "body": json.dumps(body, ensure_ascii=False),
    }
