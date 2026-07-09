import json
import boto3
from datetime import datetime, timezone

from config import REGION, SESSION_BUCKET, SESSION_PREFIX, MODEL_ID, bedrock

# ── In-process cache ──────────────────────────────────────────────────────────
_cache: dict = {}

# ── S3 helpers ────────────────────────────────────────────────────────────────

def _s3():
    return boto3.client("s3", region_name=REGION)


def _key(session_id: str) -> str:
    return f"{SESSION_PREFIX}{session_id}.json"


def _load_history(session_id: str) -> list:
    if session_id in _cache:
        return list(_cache[session_id])
    history = []
    if SESSION_BUCKET:
        try:
            obj = _s3().get_object(Bucket=SESSION_BUCKET, Key=_key(session_id))
            history = json.loads(obj["Body"].read())
        except Exception:
            pass
    _cache[session_id] = list(history)
    return history


def _save_history(session_id: str, history: list, user_id: str = "") -> None:
    _cache[session_id] = list(history)
    if not SESSION_BUCKET:
        return
    user_turns = [m for m in history if m["role"] == "user"]
    first_q    = user_turns[0]["content"][0]["text"][:80] if user_turns else ""
    _s3().put_object(
        Bucket      = SESSION_BUCKET,
        Key         = _key(session_id),
        Body        = json.dumps(history, ensure_ascii=False).encode(),
        ContentType = "application/json",
        Metadata    = {
            "user-id":        user_id or "",
            "updated-at":     datetime.now(timezone.utc).isoformat(),
            "first-question": first_q,
            "message-count":  str(len(history)),
        },
    )


# ── Public endpoint handlers ──────────────────────────────────────────────────

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
    if not SESSION_BUCKET:
        return api_response(500, {"error": "SESSION_BUCKET not configured"})
    try:
        s3     = _s3()
        paginator = s3.get_paginator("list_objects_v2")
        rows   = []
        for page in paginator.paginate(Bucket=SESSION_BUCKET, Prefix=SESSION_PREFIX):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith(".json"):
                    continue
                try:
                    meta = s3.head_object(Bucket=SESSION_BUCKET, Key=key)["Metadata"]
                    if user_id and meta.get("user-id", "") != user_id:
                        continue
                    session_id = key[len(SESSION_PREFIX):].rstrip(".json").lstrip("/").replace(".json", "")
                    rows.append({
                        "session_id":     session_id,
                        "first_question": meta.get("first-question", "(empty)"),
                        "messages":       int(meta.get("message-count", "0")),
                        "last_active":    meta.get("updated-at", ""),
                    })
                except Exception:
                    continue
        rows.sort(key=lambda x: x["last_active"], reverse=True)
        return api_response(200, rows[:100])
    except Exception as e:
        return api_response(500, {"error": str(e)})


def delete_handler(session_id: str, user_id: str = "") -> dict:  # noqa: ARG001
    from lambda_function import api_response
    try:
        _cache.pop(session_id, None)
        if SESSION_BUCKET:
            _s3().delete_object(Bucket=SESSION_BUCKET, Key=_key(session_id))
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
        _save_history(session_id, new_history, user_id)

        return api_response(200, {
            "message":        "Compacted",
            "summary":        summary,
            "original_turns": len(history),
            "new_turns":      len(new_history),
        })
    except Exception as e:
        return api_response(500, {"error": str(e)})
