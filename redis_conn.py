# app/redis_conn.py
import os
import logging
from redis import from_url, RedisError
from rq import Queue

logger = logging.getLogger(__name__)

def get_redis_conn_or_raise():
    """
    Returns a redis.StrictRedis/Redis instance constructed from REDIS_URL env var.
    Raises RuntimeError if REDIS_URL missing or connection fails.
    """
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        # Explicit failure to avoid attempts to connect to 'redis:6379'
        msg = "REDIS_URL environment variable is not set. Set REDIS_URL to your Redis connection string."
        logger.error(msg)
        raise RuntimeError(msg)

    try:
        r = from_url(redis_url, decode_responses=False)
        # quick ping to verify connection
        r.ping()
        logger.info("Connected to Redis at %s", redis_url)
        return r
    except RedisError as exc:
        logger.exception("Unable to connect to Redis at %s: %s", redis_url, exc)
        raise RuntimeError(f"Unable to connect to Redis at {redis_url}: {exc}") from exc

# Shared connection + queue to import from other modules
redis_conn = get_redis_conn_or_raise()
queue = Queue("default", connection=redis_conn)
