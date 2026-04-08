import json
import os
import uuid
from datetime import datetime, timezone

import boto3
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BEDROCK_MODEL = os.getenv("BEDROCK_MODEL", "anthropic.claude-haiku-3-5-20241022-v1:0")
BEDROCK_REGION = os.getenv("BEDROCK_REGION", "us-east-1")
S3_BUCKET = os.getenv("S3_BUCKET", "")
DYNAMODB_TABLE = os.getenv("DYNAMODB_TABLE", "")  # set to use DynamoDB instead of S3
SESSION_PREFIX = "bedrock-sessions/"

# In-memory cache to avoid redundant S3/DynamoDB reads within the same process
_cache: dict[str, list] = {}


def _load_history(session_id: str) -> list:
    if session_id in _cache:
        return _cache[session_id]
    if DYNAMODB_TABLE:
        try:
            ddb = boto3.resource("dynamodb", region_name=BEDROCK_REGION)
            item = ddb.Table(DYNAMODB_TABLE).get_item(Key={"session_id": session_id}).get("Item")
            history = json.loads(item["history"]) if item else []
        except Exception:
            history = []
    elif S3_BUCKET:
        try:
            s3 = boto3.client("s3")
            obj = s3.get_object(Bucket=S3_BUCKET, Key=f"{SESSION_PREFIX}{session_id}.json")
            history = json.loads(obj["Body"].read())
        except s3.exceptions.NoSuchKey:
            history = []
        except Exception:
            history = []
    else:
        history = []
    _cache[session_id] = history
    return history


def _save_history(session_id: str, history: list) -> None:
    _cache[session_id] = history
    if DYNAMODB_TABLE:
        try:
            ddb = boto3.resource("dynamodb", region_name=BEDROCK_REGION)
            ddb.Table(DYNAMODB_TABLE).put_item(Item={
                "session_id": session_id,
                "history": json.dumps(history),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass
    elif S3_BUCKET:
        try:
            boto3.client("s3").put_object(
                Bucket=S3_BUCKET,
                Key=f"{SESSION_PREFIX}{session_id}.json",
                Body=json.dumps(history),
                ContentType="application/json",
            )
        except Exception:
            pass


class Question(BaseModel):
    question: str
    session_id: str = ""


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ui", response_class=HTMLResponse)
def ui():
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Ask Claude</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #0f0f0f;
      color: #e0e0e0;
      height: 100vh;
      display: flex;
      flex-direction: column;
      padding: 16px;
      gap: 12px;
    }
    #input-row {
      display: flex;
      gap: 8px;
    }
    #question {
      flex: 1;
      padding: 10px 14px;
      border-radius: 8px;
      border: 1px solid #333;
      background: #1a1a1a;
      color: #e0e0e0;
      font-size: 14px;
      resize: none;
      height: 42px;
      line-height: 22px;
    }
    #question:focus { outline: none; border-color: #4a9eff; }
    #submit {
      padding: 10px 20px;
      border-radius: 8px;
      border: none;
      background: #4a9eff;
      color: white;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      white-space: nowrap;
    }
    #submit:hover { background: #2d85e8; }
    #submit:disabled { background: #333; color: #666; cursor: not-allowed; }
    #new-chat {
      padding: 10px 14px;
      border-radius: 8px;
      border: 1px solid #444;
      background: #1a1a1a;
      color: #aaa;
      font-size: 13px;
      cursor: pointer;
      white-space: nowrap;
    }
    #new-chat:hover { background: #2a2a2a; color: #e0e0e0; }
    #chat {
      flex: 1;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .bubble {
      padding: 12px 16px;
      border-radius: 10px;
      line-height: 1.6;
      font-size: 14px;
      max-width: 100%;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .user { background: #1e3a5f; align-self: flex-end; color: #c8e0ff; }
    .assistant { background: #1a1a1a; border: 1px solid #2a2a2a; color: #e0e0e0; }
    .meta {
      font-size: 11px;
      color: #555;
      padding: 2px 4px;
    }
    .spinner {
      display: inline-block;
      width: 16px; height: 16px;
      border: 2px solid #333;
      border-top-color: #4a9eff;
      border-radius: 50%;
      animation: spin 0.7s linear infinite;
      vertical-align: middle;
      margin-right: 8px;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
  </style>
</head>
<body>
  <div id="input-row">
    <textarea id="question" placeholder="Ask Claude anything… (Enter to submit)"></textarea>
    <button id="submit">Send</button>
    <button id="new-chat" title="Start a new conversation">New Chat</button>
  </div>
  <div id="chat"></div>

  <script>
    const chat = document.getElementById('chat');
    const input = document.getElementById('question');
    const btn = document.getElementById('submit');

    // Persist session across page reloads
    let sessionId = sessionStorage.getItem('bedrock_session_id') || '';

    function addBubble(text, cls, meta) {
      const wrap = document.createElement('div');
      const bubble = document.createElement('div');
      bubble.className = 'bubble ' + cls;
      bubble.textContent = text;
      wrap.appendChild(bubble);
      if (meta) {
        const m = document.createElement('div');
        m.className = 'meta';
        m.textContent = meta;
        wrap.appendChild(m);
      }
      chat.appendChild(wrap);
      chat.scrollTop = chat.scrollHeight;
      return bubble;
    }

    async function ask() {
      const q = input.value.trim();
      if (!q) return;
      input.value = '';
      btn.disabled = true;

      addBubble(q, 'user');

      const loading = addBubble('', 'assistant');
      loading.innerHTML = '<span class="spinner"></span>Thinking…';

      try {
        const res = await fetch('/ask', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ question: q, session_id: sessionId })
        });
        const data = await res.json();
        // Save session_id returned by server
        if (data.session_id) {
          sessionId = data.session_id;
          sessionStorage.setItem('bedrock_session_id', sessionId);
        }
        if (!res.ok) {
          loading.textContent = '⚠ ' + (data.detail || 'Error');
        } else {
          const answer = Array.isArray(data)
            ? data.map(r => r.answer).join('\\n\\n')
            : (data.answer || JSON.stringify(data));
          const model = Array.isArray(data) ? data[0]?.model : data.model;
          loading.textContent = answer;
          const meta = loading.nextSibling;
          if (meta && meta.className === 'meta') meta.textContent = model || '';
          else {
            const m = document.createElement('div');
            m.className = 'meta';
            m.textContent = model || '';
            loading.parentNode.appendChild(m);
          }
        }
      } catch (e) {
        loading.textContent = '⚠ Network error: ' + e.message;
      }

      btn.disabled = false;
      input.focus();
      chat.scrollTop = chat.scrollHeight;
    }

    btn.addEventListener('click', ask);
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(); }
    });
    document.getElementById('new-chat').addEventListener('click', () => {
      sessionId = '';
      sessionStorage.removeItem('bedrock_session_id');
      chat.innerHTML = '';
      input.focus();
    });
  </script>
</body>
</html>"""


@app.post("/ask")
def ask_bedrock(q: Question):
    if not q.question.strip():
        return {"question": "", "answer": "", "s3_key": None, "model": BEDROCK_MODEL, "session_id": q.session_id}

    # Resolve session
    session_id = q.session_id.strip() or str(uuid.uuid4())
    history = _load_history(session_id)

    # Append the new user message
    history.append({"role": "user", "content": q.question})

    # Call Bedrock with full conversation history
    try:
        client = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
        response = client.invoke_model(
            modelId=BEDROCK_MODEL,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 2048,
                "messages": history,
            }),
            contentType="application/json",
            accept="application/json",
        )
        body = json.loads(response["body"].read())
        answer = body["content"][0]["text"]
    except Exception as e:
        history.pop()  # remove failed user turn so it isn't stuck in history
        raise HTTPException(status_code=500, detail=f"Bedrock error: {str(e)}")

    # Append assistant reply and persist
    history.append({"role": "assistant", "content": answer})
    _save_history(session_id, history)

    # Save to S3
    s3_key = None
    if S3_BUCKET:
        try:
            s3 = boto3.client("s3")
            timestamp = datetime.now(timezone.utc).isoformat()
            s3_key = f"bedrock-qa/{timestamp}_{uuid.uuid4()}.json"
            s3.put_object(
                Bucket=S3_BUCKET,
                Key=s3_key,
                Body=json.dumps({
                    "question": q.question,
                    "answer": answer,
                    "model": BEDROCK_MODEL,
                    "timestamp": timestamp,
                }),
                ContentType="application/json",
            )
        except Exception as e:
            # S3 failure is non-fatal — still return the answer
            s3_key = f"S3 save failed: {str(e)}"

    return {
        "question": q.question,
        "answer": answer,
        "model": BEDROCK_MODEL,
        "s3_key": s3_key,
        "session_id": session_id,
    }
