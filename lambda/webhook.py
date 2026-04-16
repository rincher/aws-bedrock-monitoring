import json
import re
import time
import boto3
import urllib.request
from datetime import datetime, timezone

from config import SLACK_WEBHOOK, REGION, SESSION_TABLE
from agent import ask_with_tools

# Reuse bedrock-sessions table with alert# prefix
ALERT_PREFIX = "alert#"

ANALYSIS_PROMPT = """
Investigate this alert and provide a brief diagnosis and recommended action.
Keep the response concise — 3 to 5 bullet points maximum.
End your <response> with:
VERDICT: TRUE_ALERT     — real problem requiring action
VERDICT: FALSE_POSITIVE — transient spike, self-resolving
"""


def handle_webhook(payload: dict, async_mode: bool = False) -> dict:
    if not async_mode:
        _invoke_async(payload)
        return {"status": "accepted"}

    for alert in payload.get("alerts", []):
        status      = alert.get("status")
        labels      = alert.get("labels", {})
        values      = alert.get("values", {})
        fingerprint = alert.get("fingerprint", "unknown")
        alert_name  = labels.get("alertname", "Alert")
        instance_id = labels.get("InstanceId", labels.get("instance_id", ""))
        metric_val  = next(iter(values.values()), None)
        value_str   = f"{float(metric_val):.1f}%" if metric_val is not None else "unknown"

        if status == "resolved":
            _delete_alert_state(fingerprint)
            _send_slack_resolved(alert_name, instance_id)
            continue

        if status != "firing":
            continue

        state = _get_alert_state(fingerprint)

        if state is None:
            # First time firing — send immediate notification, store state
            _put_alert_state(fingerprint, alert_name, instance_id, value_str)
            _send_slack_notification(alert_name, instance_id, value_str)
        else:
            # Still firing — run Bedrock analysis
            question = (
                f"ALERT: {alert_name} on {instance_id or 'EC2'} "
                f"has been firing for over 5 minutes with value {value_str}. "
                f"Investigate the root cause and provide recommended actions."
                + ANALYSIS_PROMPT
            )
            analysis = ask_with_tools(question, [])
            if _parse_verdict(analysis):
                _send_slack_analysis(alert_name, instance_id, value_str, analysis)

    return {"status": "ok"}


# ── DynamoDB helpers ───────────────────────────────────────────────────────────

def _get_alert_state(fingerprint: str) -> dict | None:
    ddb = boto3.resource("dynamodb", region_name=REGION)
    table = ddb.Table(SESSION_TABLE)
    resp = table.get_item(Key={"session_id": ALERT_PREFIX + fingerprint})
    return resp.get("Item")


def _put_alert_state(fingerprint: str, alert_name: str, instance_id: str, value: str):
    ddb = boto3.resource("dynamodb", region_name=REGION)
    table = ddb.Table(SESSION_TABLE)
    table.put_item(Item={
        "session_id": ALERT_PREFIX + fingerprint,
        "alert_name": alert_name,
        "instance_id": instance_id,
        "value": value,
        "fired_at": int(time.time()),
        # TTL: auto-expire after 24h in case resolved webhook is missed
        "ttl": int(time.time()) + 86400,
    })


def _delete_alert_state(fingerprint: str):
    ddb = boto3.resource("dynamodb", region_name=REGION)
    table = ddb.Table(SESSION_TABLE)
    table.delete_item(Key={"session_id": ALERT_PREFIX + fingerprint})


# ── Verdict parsing ────────────────────────────────────────────────────────────

def _parse_verdict(analysis: str) -> bool:
    match = re.search(r'VERDICT:\s*(TRUE_ALERT|FALSE_POSITIVE)', analysis, re.IGNORECASE)
    if match:
        return match.group(1).upper() == "TRUE_ALERT"
    return True  # fail-safe


# ── Slack messages ─────────────────────────────────────────────────────────────

def _send_slack_notification(alert_name: str, instance_id: str, value: str):
    """Stage 1: simple firing notification."""
    _post_slack({
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"🚨 {alert_name}"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Instance:*\n{instance_id or 'N/A'}"},
                    {"type": "mrkdwn", "text": f"*Value:*\n{value}"},
                ]
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn",
                               "text": "⏳ Monitoring — AI analysis will follow if alert persists for 5 minutes"}]
            }
        ]
    })


def _send_slack_analysis(alert_name: str, instance_id: str, value: str, analysis: str):
    """Stage 2: Bedrock AI analysis after 5 min."""
    r_match = re.search(r'<response>([\s\S]*)</response>', analysis, re.IGNORECASE)
    clean = r_match.group(1).strip() if r_match else analysis
    clean = re.sub(r'\nVERDICT:.*', '', clean).strip()

    _post_slack({
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"🔍 AI Analysis — {alert_name}"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Instance:*\n{instance_id or 'N/A'}"},
                    {"type": "mrkdwn", "text": f"*Value:*\n{value}"},
                ]
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": clean[:2800]}
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "Powered by Amazon Bedrock"}]
            }
        ]
    })


def _send_slack_resolved(alert_name: str, instance_id: str):
    """Resolved notification."""
    _post_slack({
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn",
                         "text": f"✅ *{alert_name}* resolved\nInstance: {instance_id or 'N/A'}"}
            }
        ]
    })


def _post_slack(payload: dict):
    if not SLACK_WEBHOOK:
        print("SLACK_WEBHOOK not configured")
        return
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        SLACK_WEBHOOK,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"Slack sent: {resp.status}")
    except Exception as e:
        print(f"Slack failed: {e}")


# ── Async invocation ───────────────────────────────────────────────────────────

def _invoke_async(payload: dict):
    lam = boto3.client("lambda", region_name=REGION)
    lam.invoke(
        FunctionName="bedrock-qa",
        InvocationType="Event",
        Payload=json.dumps({"_async_webhook": True, "payload": payload}).encode(),
    )
