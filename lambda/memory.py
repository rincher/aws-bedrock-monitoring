"""
Long-term memory layer.

Facts are stored in a single DynamoDB item (memory_id = "global").
They are extracted automatically after each Q&A turn and injected
into the system prompt of every new session.
"""
import json
import boto3
from datetime import datetime, timezone

from config import REGION, MODEL_ID, bedrock

MEMORY_TABLE = "bedrock-memory"
MEMORY_ID    = "global"
MAX_FACTS    = 50   # cap to keep system prompt reasonable


def _table():
    return boto3.resource("dynamodb", region_name=REGION).Table(MEMORY_TABLE)


def get_facts() -> list[str]:
    """Return the current list of stored facts (empty list if none yet)."""
    try:
        item = _table().get_item(Key={"memory_id": MEMORY_ID}).get("Item")
        return item.get("facts", []) if item else []
    except Exception:
        return []


def save_facts(facts: list[str]) -> None:
    try:
        _table().put_item(Item={
            "memory_id":  MEMORY_ID,
            "facts":      facts[-MAX_FACTS:],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass


def delete_fact(index: int) -> list[str]:
    facts = get_facts()
    if 0 <= index < len(facts):
        facts.pop(index)
        save_facts(facts)
    return facts


def extract_and_store(question: str, answer: str) -> None:
    """
    Ask the model to pull out 0-3 facts worth remembering from one Q&A exchange.
    Called after every /ask turn — runs synchronously but is lightweight.
    """
    prompt = (
        "Extract 0 to 3 concise facts worth remembering long-term from this exchange. "
        "Focus on facts about the user's AWS environment, preferences, or recurring concerns. "
        "Skip generic or one-off answers. "
        "Return ONLY a JSON array of short strings, e.g. [\"fact1\", \"fact2\"]. "
        "Return [] if nothing is worth remembering.\n\n"
        f"User: {question[:400]}\n"
        f"Assistant: {answer[:600]}"
    )
    try:
        resp = bedrock.converse(
            modelId=MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
        )
        raw = resp["output"]["message"]["content"][0]["text"].strip()
        # Parse JSON array from the response
        start = raw.find("[")
        end   = raw.rfind("]") + 1
        new_facts = json.loads(raw[start:end]) if start != -1 else []
        if not isinstance(new_facts, list):
            return
        new_facts = [str(f).strip() for f in new_facts if str(f).strip()]
        if not new_facts:
            return
        existing = get_facts()
        # Deduplicate: skip facts already substantially present
        for fact in new_facts:
            if not any(fact.lower()[:40] in e.lower() for e in existing):
                existing.append(fact)
        save_facts(existing)
    except Exception:
        pass  # memory extraction is non-critical


def build_memory_prompt(facts: list[str]) -> str:
    """Format facts as a system prompt section."""
    if not facts:
        return ""
    lines = "\n".join(f"- {f}" for f in facts)
    return f"\n\nLong-term memory (facts from past conversations):\n{lines}"
