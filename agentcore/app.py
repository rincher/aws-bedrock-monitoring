import json
import os
import re
import uuid
from datetime import datetime, timezone

import boto3
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from config import MEMORY_ID, SESSION_TABLE, REGION, agentcore_data
from agent_loop import ask_with_tools, MODEL_SONNET

app = FastAPI()

_ddb = boto3.resource("dynamodb", region_name=REGION)

_MYSQL_PATTERN = re.compile(
    r'\b(select|insert|update|delete|'
    r'join|innodb|varchar|stored.?proc|trigger|'
    r'clothing_store|show\s+tables|primary\s+key|foreign\s+key)\b'
    r'|(?:테이블|데이터베이스|스키마|컬럼|레코드).{0,10}(조회|확인|검색|가져|뽑|쿼리)',
    re.IGNORECASE,
)


def _resolve_model(question: str, requested: str | None) -> str | None:
    if _MYSQL_PATTERN.search(question):
        return MODEL_SONNET
    return requested or None


def _load_history(session_id: str, actor_id: str) -> list:
    """Load conversation turns from AgentCore Memory short-term events."""
    if not MEMORY_ID:
        return []
    try:
        resp = agentcore_data.list_events(
            memoryId=MEMORY_ID,
            actorId=actor_id or "anonymous",
            sessionId=session_id,
        )
        history = []
        for ev in sorted(resp.get("events", []), key=lambda e: e.get("createdAt", "")):
            ch = ev.get("conversationHistory", {})
            role    = ch.get("role")
            content = ch.get("content", [])
            if role and content:
                history.append({"role": role, "content": content})
        return history
    except Exception:
        return []


def _save_turn(session_id: str, actor_id: str, question: str, answer: str) -> None:
    """Persist user + assistant turns as AgentCore Memory events."""
    if not MEMORY_ID:
        return
    actor_id = actor_id or "anonymous"
    for role, text in [("user", question), ("assistant", answer)]:
        try:
            agentcore_data.create_event(
                memoryId=MEMORY_ID,
                actorId=actor_id,
                sessionId=session_id,
                event={
                    "conversationHistory": {
                        "role": role,
                        "content": [{"text": text}],
                    }
                },
            )
        except Exception:
            pass


def _update_session_metadata(session_id: str, user_id: str, question: str) -> None:
    """Keep a lightweight DynamoDB record for the session list sidebar."""
    try:
        item = {
            "session_id": session_id,
            "user_id":    user_id or "",
            "first_question": question[:80],
            "last_active": datetime.now(timezone.utc).isoformat(),
            "source": "agentcore",
        }
        # Only write first_question on first turn (don't overwrite on subsequent turns)
        _ddb.Table(SESSION_TABLE).update_item(
            Key={"session_id": session_id},
            UpdateExpression=(
                "SET last_active = :t, user_id = if_not_exists(user_id, :u), "
                "first_question = if_not_exists(first_question, :q), #src = :s"
            ),
            ExpressionAttributeNames={"#src": "source"},
            ExpressionAttributeValues={
                ":t": item["last_active"],
                ":u": item["user_id"],
                ":q": item["first_question"],
                ":s": "agentcore",
            },
        )
    except Exception:
        pass


@app.get("/ping")
def ping():
    return {"status": "ok"}


@app.post("/invocations")
async def invocations(request: Request):
    body = await request.json()
    question = body.get("question", "").strip()
    if not question:
        return JSONResponse({"error": "question is required"}, status_code=400)

    session_id = body.get("session_id", "").strip() or str(uuid.uuid4())
    user_id    = body.get("user_id",    "").strip()
    model      = _resolve_model(question, body.get("model") or None)

    history = _load_history(session_id, user_id)
    answer, used_model = ask_with_tools(question, history, model=model)

    _save_turn(session_id, user_id, question, answer)
    _update_session_metadata(session_id, user_id, question)

    return {"answer": answer, "model": used_model, "session_id": session_id}
