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

# Only the head of the queue may be claimed, and only if the deployment has a free slot AND (when the
# deployment's node has a concurrency budget) the node as a whole has room: strict FIFO per queue.
CLAIM_LUA = """
local head = redis.call('ZRANGE', KEYS[1], 0, 0)
if head[1] ~= ARGV[1] then return 0 end
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[2]) then return 0 end
local nmax = tonumber(ARGV[4])
if KEYS[3] ~= '' and nmax > 0 and redis.call('ZCARD', KEYS[3]) >= nmax then return 0 end
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZADD', KEYS[2], tonumber(ARGV[3]), ARGV[1])
if KEYS[3] ~= '' then
  redis.call('ZADD', KEYS[3], tonumber(ARGV[3]), ARGV[1])
  redis.call('SET', 'nexora:rnode:' .. ARGV[1], ARGV[5], 'EX', 86400)
end
return 1
"""

RELEASE_LUA = """
redis.call('ZREM', KEYS[1], ARGV[1])
local n = redis.call('GET', 'nexora:rnode:' .. ARGV[1])
if n then
  redis.call('ZREM', 'nexora:nodeactive:' .. n, ARGV[1])
  redis.call('DEL', 'nexora:rnode:' .. ARGV[1])
end
return 1
"""

# Move a still-queued request to another deployment's queue (assigning a fresh sequence there). If the
# destination is full the request goes back to its original place.
MOVE_LUA = """
local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not score then return -2 end
redis.call('ZREM', KEYS[1], ARGV[1])
if redis.call('ZCARD', KEYS[3]) >= tonumber(ARGV[2]) then
  redis.call('ZADD', KEYS[1], score, ARGV[1])
  return -1
end
local seq = redis.call('INCR', KEYS[2])
redis.call('ZADD', KEYS[3], seq, ARGV[1])
return seq
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

    async def try_claim(self, dep, rid, max_active: int, node_id: str | None = None,
                        node_max: int = 0) -> bool:
        ok = await self.redis.eval(CLAIM_LUA, 3, self._q(dep), self._a(dep),
                                   f"nexora:nodeactive:{node_id}" if node_id else "", str(rid), max_active,
                                   time.time() + self.lease, node_max or 0, node_id or "")
        return bool(ok)

    async def move_queued(self, old_dep, new_dep, rid, max_queue: int) -> int:
        """Returns the new sequence, -1 if the destination is full, -2 if rid was no longer queued."""
        return int(await self.redis.eval(MOVE_LUA, 3, self._q(old_dep), f"nexora:seq:{new_dep}",
                                         self._q(new_dep), str(rid), max_queue))

    async def set_moved(self, rid, info: dict) -> None:
        import json

        await self.redis.set(f"nexora:moved:{rid}", json.dumps(info), ex=3600)

    async def get_moved(self, rid) -> dict | None:
        import json

        raw = await self.redis.get(f"nexora:moved:{rid}")
        return json.loads(raw) if raw else None

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
        await self.redis.eval(RELEASE_LUA, 1, self._a(dep), str(rid))

    async def remove_queued(self, dep, rid) -> bool:
        return bool(await self.redis.zrem(self._q(dep), str(rid)))

    async def request_cancel(self, rid) -> None:
        await self.redis.set(f"nexora:cancel:{rid}", 1, ex=3600)

    async def cancel_requested(self, rid) -> bool:
        return bool(await self.redis.exists(f"nexora:cancel:{rid}"))

    async def clear(self, rid) -> None:
        await self.redis.delete(f"nexora:waiter:{rid}", f"nexora:cancel:{rid}", f"nexora:moved:{rid}")

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
