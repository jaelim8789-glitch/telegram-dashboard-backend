import json
from typing import Optional
import redis.asyncio as aioredis

redis_client: Optional[aioredis.Redis] = None


async def init_redis():
    global redis_client
    try:
        redis_client = aioredis.from_url("redis://localhost:6379/0", decode_responses=True)
        await redis_client.ping()
    except Exception:
        redis_client = None


async def cache_get(key: str) -> Optional[dict]:
    if not redis_client:
        return None
    try:
        val = await redis_client.get(f"dashboard:{key}")
        return json.loads(val) if val else None
    except Exception:
        return None


async def cache_set(key: str, data: dict, ttl: int = 60):
    if not redis_client:
        return
    try:
        await redis_client.setex(f"dashboard:{key}", ttl, json.dumps(data))
    except Exception:
        pass
