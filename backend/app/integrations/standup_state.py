"""Redis-backed ephemeral state for the Telegram standup flow.

- getUpdates offset (so restarts don't reprocess the backlog)
- one-time registration link codes
- per-chat conversation progress (which standup question is pending)

Durable answers live in the DB (StandupEntry); only hot/ephemeral progress is
kept here.
"""

import json
import secrets

import redis.asyncio as aioredis

from app.config import settings

LINK_CODE_TTL = 900  # 15 minutes
CONVO_TTL = 60 * 60 * 6  # 6 hours

_OFFSET_KEY = "telegram:offset"


def get_redis() -> aioredis.Redis:
    """Create a Redis client (decode responses to str)."""
    return aioredis.from_url(settings.redis_url, decode_responses=True)


# --- getUpdates offset -------------------------------------------------------
async def get_offset(r: aioredis.Redis) -> int | None:
    val = await r.get(_OFFSET_KEY)
    return int(val) if val is not None else None


async def set_offset(r: aioredis.Redis, offset: int) -> None:
    await r.set(_OFFSET_KEY, offset)


# --- registration link codes -------------------------------------------------
async def create_link_code(r: aioredis.Redis, member_id: str) -> str:
    code = secrets.token_hex(3)  # 6 hex chars
    await r.set(f"telegram:linkcode:{code}", member_id, ex=LINK_CODE_TTL)
    return code


async def resolve_link_code(r: aioredis.Redis, code: str) -> str | None:
    """Return the member_id for a code and consume it (one-time use)."""
    key = f"telegram:linkcode:{code}"
    member_id = await r.get(key)
    if member_id is not None:
        await r.delete(key)
    return member_id


# --- per-chat conversation progress -----------------------------------------
def _convo_key(chat_id: str) -> str:
    return f"standup:convo:{chat_id}"


async def set_convo(r: aioredis.Redis, chat_id: str, state: dict) -> None:
    await r.set(_convo_key(chat_id), json.dumps(state), ex=CONVO_TTL)


async def get_convo(r: aioredis.Redis, chat_id: str) -> dict | None:
    val = await r.get(_convo_key(chat_id))
    return json.loads(val) if val else None


async def clear_convo(r: aioredis.Redis, chat_id: str) -> None:
    await r.delete(_convo_key(chat_id))


# --- /ask conversation memory ------------------------------------------------
ASK_TTL = 60 * 60 * 24  # 24 hours


def _ask_key(chat_id: str) -> str:
    return f"telegram:ask:{chat_id}"


async def get_ask_history(r: aioredis.Redis, chat_id: str) -> list[dict] | None:
    val = await r.get(_ask_key(chat_id))
    return json.loads(val) if val else None


async def set_ask_history(r: aioredis.Redis, chat_id: str, turns: list[dict]) -> None:
    """Persist only the last 3 user+assistant pairs (6 messages)."""
    await r.set(_ask_key(chat_id), json.dumps(turns[-6:]), ex=ASK_TTL)


async def delete_ask_history(r: aioredis.Redis, chat_id: str) -> None:
    await r.delete(_ask_key(chat_id))
