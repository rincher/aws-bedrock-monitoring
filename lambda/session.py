import json
import boto3
from boto3.dynamodb.conditions import Attr
from datetime import datetime, timezone

from config import REGION, SESSION_TABLE, SESSION_BUCKET, SESSION_PREFIX, MODEL_ID, bedrock

_REQ_PREFIX = "_req_"


# ── Async request tracking ────────────────────────────────────────────────────

def save_async_request(request_id: str, question: str, session_id: str, user_id: str, model: str) -> None:
    ddb = boto3.resource("dynamodb", region_name=REGION)
    ddb.Table(SESSION_TABLE).put_item(Item={
        "session_id":     f"{_REQ_PREFIX}{request_id}",
        "status":         "pending",
        "question":       question,
        "req_session_id": session_id,
        "user_id":        user_id or "",
        "model":          model or "",
        "updated_at":     datetime.now(timezone.utc).isoformat(),
    })


def complete_async_request(request_id: str, answer: str, model: str) -> None:
    ddb = boto3.resource("dynamodb", region_name=REGION)
    ddb.Table(SESSION_TABLE).update_item(
        Key={"session_id": f"{_REQ_PREFIX}{request_id}"},
        UpdateExpression="SET #s=:s, answer=:a, model=:m, updated_at=:t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": "done", ":a": answer, ":m": model,
            ":t": datetime.now(timezone.utc).isoformat(),
        },
    )


def fail_async_request(request_id: str, error: str) -> None:
    ddb = boto3.resource("dynamodb", region_name=REGION)
    ddb.Table(SESSION_TABLE).update_item(
        Key={"session_id": f"{_REQ_PREFIX}{request_id}"},
        UpdateExpression="SET #s=:s, #e=:e, updated_at=:t",
        ExpressionAttributeNames={"#s": "status", "#e": "error"},
        ExpressionAttributeValues={
            ":s": "error", ":e": error,
            ":t": datetime.now(timezone.utc).isoformat(),
        },
    )


def get_async_request(request_id: str) -> dict:
    ddb = boto3.resource("dynamodb", region_name=REGION)
    return ddb.Table(SESSION_TABLE).get_item(
        Key={"session_id": f"{_REQ_PREFIX}{request_id}"}
    ).get("Item") or {}

# In-process cache to avoid redundant DynamoDB reads within the same container
_cache: dict = {}


def _load_history(session_id: str) -> list:
    if session_id in _cache:
        return list(_cache[session_id])
    history = []
    try:
        ddb = boto3.resource("dynamodb", region_name=REGION)
        item = ddb.Table(SESSION_TABLE).get_item(Key={"session_id": session_id}).get("Item")
        if item:
            history = json.loads(item["history"])
    except Exception:
        if SESSION_BUCKET:
            try:
                s3 = boto3.client("s3")
                obj = s3.get_object(Bucket=SESSION_BUCKET, Key=f"{SESSION_PREFIX}{session_id}.json")
                history = json.loads(obj["Body"].read())
            except Exception:
                pass
    _cache[session_id] = list(history)
    return history


def _save_history(session_id: str, history: list, user_id: str = "") -> None:
    _cache[session_id] = list(history)
    try:
        ddb = boto3.resource("dynamodb", region_name=REGION)
        item = {
            "session_id": session_id,
            "history": json.dumps(history, ensure_ascii=False),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if user_id:
            item["user_id"] = user_id
        ddb.Table(SESSION_TABLE).put_item(Item=item)
        return
    except Exception:
        pass
    if SESSION_BUCKET:
        try:
            boto3.client("s3").put_object(
                Bucket=SESSION_BUCKET,
                Key=f"{SESSION_PREFIX}{session_id}.json",
                Body=json.dumps(history, ensure_ascii=False),
                ContentType="application/json",
            )
        except Exception:
            pass


def session_handler(session_id: str) -> dict:
    from lambda_function import api_response
    try:
        history = _load_history(session_id)
        turns = [
            {"role": m["role"], "text": m["content"][0]["text"] if m.get("content") else ""}
            for m in history
        ]
        return api_response(200, {"session_id": session_id, "turns": turns})
    except Exception as e:
        return api_response(500, {"error": str(e)})


def history_handler(user_id: str = "") -> dict:
    from lambda_function import api_response
    try:
        ddb = boto3.resource("dynamodb", region_name=REGION)
        kwargs = {
            "ProjectionExpression": "session_id, updated_at, history, user_id",
            "Limit": 100,
        }
        # Exclude async request tracking records (no history field)
        f = Attr("history").exists()
        if user_id:
            f = f & Attr("user_id").eq(user_id)
        kwargs["FilterExpression"] = f
        result = ddb.Table(SESSION_TABLE).scan(**kwargs)
        rows = []
        for item in result.get("Items", []):
            try:
                hist = json.loads(item.get("history", "[]"))
                user_turns = [m for m in hist if m["role"] == "user"]
                first_q = user_turns[0]["content"][0]["text"][:80] if user_turns else "(empty)"
                rows.append({
                    "session_id": item["session_id"],
                    "first_question": first_q,
                    "messages": len(hist),
                    "last_active": item.get("updated_at", ""),
                })
            except Exception:
                continue
        rows.sort(key=lambda x: x["last_active"], reverse=True)
        return api_response(200, rows)
    except Exception as e:
        return api_response(500, {"error": str(e)})


def delete_handler(session_id: str, user_id: str = "") -> dict:  # noqa: ARG001
    from lambda_function import api_response
    try:
        _cache.pop(session_id, None)
        ddb = boto3.resource("dynamodb", region_name=REGION)
        ddb.Table(SESSION_TABLE).delete_item(Key={"session_id": session_id})
        if SESSION_BUCKET:
            try:
                boto3.client("s3").delete_object(
                    Bucket=SESSION_BUCKET,
                    Key=f"{SESSION_PREFIX}{session_id}.json",
                )
            except Exception:
                pass
        return api_response(200, {"message": "Deleted", "session_id": session_id})
    except Exception as e:
        return api_response(500, {"error": str(e)})


def compact_handler(session_id: str, user_id: str = "") -> dict:  # noqa: ARG001
    from lambda_function import api_response
    try:
        history = _load_history(session_id)
        if len(history) < 4:
            return api_response(200, {"message": "Nothing to compact (fewer than 4 turns)", "turns": len(history)})

        lines = []
        for m in history:
            role = "User" if m["role"] == "user" else "Assistant"
            text = m["content"][0]["text"] if m.get("content") else ""
            lines.append(role + ": " + text[:500])

        prompt = (
            "Summarise the following conversation into a single concise paragraph "
            "that preserves all key facts, decisions, and context needed to continue. "
            "Output only the summary, no preamble.\n\n" + "\n".join(lines)
        )

        resp = bedrock.converse(
            modelId=MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
        )
        summary = resp["output"]["message"]["content"][0]["text"]

        new_history = [
            {"role": "user",      "content": [{"text": "[Conversation summary] " + summary}]},
            {"role": "assistant", "content": [{"text": "Got it. I have the context from our previous conversation. How can I help?"}]},
        ]
        _save_history(session_id, new_history)

        return api_response(200, {
            "message": "Compacted",
            "summary": summary,
            "original_turns": len(history),
            "new_turns": len(new_history),
        })
    except Exception as e:
        return api_response(500, {"error": str(e)})
