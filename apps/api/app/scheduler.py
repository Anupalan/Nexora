"""Redis-backed FIFO admission scheduler, one queue per model deployment (roadmap §5.6, §21).

FIFO is guaranteed per deployment queue, at the admission/claim step. vLLM still batches
generation internally, so completion order across concurrent requests is not promised.

Redis structures (all keys are `nexora:*`):
  seq:{dep}     INCR counter giving a monotonically increasing sequence
  queue:{dep}   ZSET  member=request_id, score=sequence        (waiting; holds NO inference slot)
  active:{dep}  ZSET  member=request_id, score=lease expiry    (running; occupies a slot)
  waiter:{rid}  string with TTL, refreshed by the process holding the client connection
  cancel:{rid}  flag set by the cancel API for a running request
"""

import time

ENQUEUE_LUA = """
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[2]) then return -1 end
local seq = redis.call('INCR', KEYS[1])
redis.call('ZADD', KEYS[2], seq, ARGV[1])
return seq
"""

# Only the head of the queue may be claimed, and only if a slot is free: strict FIFO.
CLAIM_LUA = """
local head = redis.call('ZRANGE', KEYS[1], 0, 0)
if head[1] ~= ARGV[1] then return 0 end
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[2]) then return 0 end
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZADD', KEYS[2], tonumber(ARGV[3]), ARGV[1])
return 1
"""


class Scheduler:
    def __init__(self, redis, *, lease_seconds: int = 60, waiter_ttl_seconds: int = 15):
        self.redis = redis
        self.lease = lease_seconds
        self.waiter_ttl = waiter_ttl_seconds

    @staticmethod
    def _q(dep) -> str:
        return f"nexora:queue:{dep}"

    @staticmethod
    def _a(dep) -> str:
        return f"nexora:active:{dep}"

    async def enqueue(self, dep, rid, max_queue: int) -> int:
        """Atomically assign a sequence and enqueue. Returns the sequence, or -1 if the queue is full."""
        seq = await self.redis.eval(ENQUEUE_LUA, 2, f"nexora:seq:{dep}", self._q(dep), str(rid), max_queue)
        await self.touch_waiter(rid)
        return int(seq)

    async def try_claim(self, dep, rid, max_active: int) -> bool:
        ok = await self.redis.eval(CLAIM_LUA, 2, self._q(dep), self._a(dep), str(rid), max_active,
                                   time.time() + self.lease)
        return bool(ok)

    async def position(self, dep, rid) -> int | None:
        """1 = next to be claimed; None if not queued."""
        rank = await self.redis.zrank(self._q(dep), str(rid))
        return None if rank is None else rank + 1

    async def touch_waiter(self, rid) -> None:
        await self.redis.set(f"nexora:waiter:{rid}", 1, ex=self.waiter_ttl)

    async def waiter_alive(self, rid) -> bool:
        return bool(await self.redis.exists(f"nexora:waiter:{rid}"))

    async def heartbeat_active(self, dep, rid) -> None:
        await self.redis.zadd(self._a(dep), {str(rid): time.time() + self.lease}, xx=True)

    async def release_active(self, dep, rid) -> None:
        await self.redis.zrem(self._a(dep), str(rid))

    async def remove_queued(self, dep, rid) -> bool:
        return bool(await self.redis.zrem(self._q(dep), str(rid)))

    async def request_cancel(self, rid) -> None:
        await self.redis.set(f"nexora:cancel:{rid}", 1, ex=3600)

    async def cancel_requested(self, rid) -> bool:
        return bool(await self.redis.exists(f"nexora:cancel:{rid}"))

    async def clear(self, rid) -> None:
        await self.redis.delete(f"nexora:waiter:{rid}", f"nexora:cancel:{rid}")

    async def depth(self, dep) -> int:
        return int(await self.redis.zcard(self._q(dep)))

    async def active_count(self, dep) -> int:
        return int(await self.redis.zcard(self._a(dep)))

    @staticmethod
    def _str(members) -> list[str]:
        return [m.decode() if isinstance(m, bytes) else m for m in members]

    async def queued_ids(self, dep) -> list[str]:
        return self._str(await self.redis.zrange(self._q(dep), 0, -1))

    async def expired_leases(self, dep) -> list[str]:
        ids = await self.redis.zrangebyscore(self._a(dep), "-inf", time.time())
        return self._str(ids)

    async def active_ids(self, dep) -> list[str]:
        return self._str(await self.redis.zrange(self._a(dep), 0, -1))
