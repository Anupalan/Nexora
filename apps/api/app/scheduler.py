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

# Keys (all `nexora:*`):
#   queue:{dep}        ZSET all waiting requests, score = sequence (position, depth, recovery)
#   cq:{dep}:{class}   ZSET the same entries split by workload class (claim looks only at each class's head)
#   qclass:{rid}       the request's class while it waits; rclass:{rid} while it runs
#   aclass:{dep}       HASH running requests per class;  seen:{dep}:{class}  recent-arrival marker (TTL)
ENQUEUE_LUA = """
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[2]) then return -1 end
local seq = redis.call('INCR', KEYS[1])
redis.call('ZADD', KEYS[2], seq, ARGV[1])
redis.call('ZADD', KEYS[3], seq, ARGV[1])
redis.call('SET', 'nexora:qclass:' .. ARGV[1], ARGV[3], 'EX', 86400)
if tonumber(ARGV[4]) > 0 then redis.call('SET', KEYS[4], 1, 'EX', tonumber(ARGV[4])) end
return seq
"""

# One atomic decision for the whole deployment: which waiting request gets the next free slot.
# Every waiter runs the same script on its own turn, so they all agree on the winner.
#   1. no free slot (deployment or node budget) -> nobody
#   2. candidates = the head of each class's queue (FIFO inside a class)
#   3. rank: classes still under their reservation first, then lower priority number, then older sequence
#   4. a class under its reservation may take any free slot; a class over it is only borrowing and may
#      take a slot only if that leaves the slots held back for other recently-active classes
CLAIM_LUA = """
local cfg = cjson.decode(ARGV[6])
local idle = tonumber(cfg['_idle'] or 0)
local max_active = tonumber(ARGV[2])
local total = redis.call('ZCARD', KEYS[2])
if total >= max_active then return 0 end
local nmax = tonumber(ARGV[4])
if KEYS[3] ~= '' and nmax > 0 and redis.call('ZCARD', KEYS[3]) >= nmax then return 0 end

local act = {}
local h = redis.call('HGETALL', KEYS[4])
for i = 1, #h, 2 do act[h[i]] = tonumber(h[i + 1]) end
local names = {}
for name, _ in pairs(cfg) do if name ~= '_idle' then names[#names + 1] = name end end
table.sort(names)

local head, held, held_total = {}, {}, 0
for _, c in ipairs(names) do
  local first = redis.call('ZRANGE', 'nexora:cq:' .. ARGV[7] .. ':' .. c, 0, 0, 'WITHSCORES')
  if first[1] then head[c] = {first[1], tonumber(first[2])} end
  local a = act[c] or 0
  local r = tonumber(cfg[c]['r'])
  local seen = (a > 0) or (head[c] ~= nil)
  if not seen and idle > 0 then
    seen = redis.call('EXISTS', 'nexora:seen:' .. ARGV[7] .. ':' .. c) == 1
  end
  held[c] = 0
  if idle > 0 and seen and r > a then held[c] = r - a end
  held_total = held_total + held[c]
end

local cands = {}
for _, c in ipairs(names) do
  if head[c] then
    local a = act[c] or 0
    local r = tonumber(cfg[c]['r'])
    cands[#cands + 1] = {
      c = c, under = (a < r) and 0 or 1, p = tonumber(cfg[c]['p']),
      seq = head[c][2], id = head[c][1], r = r, a = a,
    }
  end
end
table.sort(cands, function(x, y)
  if x.under ~= y.under then return x.under < y.under end
  if x.p ~= y.p then return x.p < y.p end
  return x.seq < y.seq
end)

local free = max_active - total
-- Holding must never shut every slot: keep at least one borrowable, or a lone slot would sit idle
-- (and extraction starve) for the whole window after any chat request.
local held_eff = math.min(held_total, max_active - 1)
for _, k in ipairs(cands) do
  local ok
  if k.a < k.r then ok = free >= 1 else ok = (free - 1) >= (held_eff - math.min(held[k.c], held_eff)) end
  if ok then
    if k.id ~= ARGV[1] then return 0 end
    redis.call('ZREM', KEYS[1], k.id)
    redis.call('ZREM', 'nexora:cq:' .. ARGV[7] .. ':' .. k.c, k.id)
    redis.call('ZADD', KEYS[2], tonumber(ARGV[3]), k.id)
    redis.call('HINCRBY', KEYS[4], k.c, 1)
    redis.call('SET', 'nexora:rclass:' .. k.id, k.c, 'EX', 86400)
    if KEYS[3] ~= '' then
      redis.call('ZADD', KEYS[3], tonumber(ARGV[3]), k.id)
      redis.call('SET', 'nexora:rnode:' .. k.id, ARGV[5], 'EX', 86400)
    end
    return 1
  end
end
return 0
"""

RELEASE_LUA = """
if redis.call('ZREM', KEYS[1], ARGV[1]) == 1 then
  local c = redis.call('GET', 'nexora:rclass:' .. ARGV[1])
  if c then
    local n = redis.call('HINCRBY', 'nexora:aclass:' .. ARGV[2], c, -1)
    if n < 0 then redis.call('HSET', 'nexora:aclass:' .. ARGV[2], c, 0) end
    redis.call('DEL', 'nexora:rclass:' .. ARGV[1])
  end
end
local n = redis.call('GET', 'nexora:rnode:' .. ARGV[1])
if n then
  redis.call('ZREM', 'nexora:nodeactive:' .. n, ARGV[1])
  redis.call('DEL', 'nexora:rnode:' .. ARGV[1])
end
return 1
"""

REMOVE_LUA = """
local c = redis.call('GET', 'nexora:qclass:' .. ARGV[1])
local n = redis.call('ZREM', KEYS[1], ARGV[1])
if n == 1 and c then redis.call('ZREM', 'nexora:cq:' .. ARGV[2] .. ':' .. c, ARGV[1]) end
return n
"""

# Move a still-queued request to another deployment's queue (fresh sequence there, same class). If the
# destination is full the request goes back to its original place.
MOVE_LUA = """
local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not score then return -2 end
local c = redis.call('GET', 'nexora:qclass:' .. ARGV[1])
redis.call('ZREM', KEYS[1], ARGV[1])
if c then redis.call('ZREM', 'nexora:cq:' .. ARGV[3] .. ':' .. c, ARGV[1]) end
if redis.call('ZCARD', KEYS[3]) >= tonumber(ARGV[2]) then
  redis.call('ZADD', KEYS[1], score, ARGV[1])
  if c then redis.call('ZADD', 'nexora:cq:' .. ARGV[3] .. ':' .. c, score, ARGV[1]) end
  return -1
end
local seq = redis.call('INCR', KEYS[2])
redis.call('ZADD', KEYS[3], seq, ARGV[1])
if c then
  redis.call('ZADD', 'nexora:cq:' .. ARGV[4] .. ':' .. c, seq, ARGV[1])
  if tonumber(ARGV[5]) > 0 then redis.call('SET', 'nexora:seen:' .. ARGV[4] .. ':' .. c, 1, 'EX', tonumber(ARGV[5])) end
end
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

    async def enqueue(self, dep, rid, max_queue: int, klass: str = "standard", idle_s: int = 0) -> int:
        """Atomically assign a sequence and enqueue. Returns the sequence, or -1 if the queue is full."""
        seq = await self.redis.eval(ENQUEUE_LUA, 4, f"nexora:seq:{dep}", self._q(dep), f"nexora:cq:{dep}:{klass}",
                                    f"nexora:seen:{dep}:{klass}", str(rid), max_queue, klass, idle_s)
        await self.touch_waiter(rid)
        return int(seq)

    async def try_claim(self, dep, rid, max_active: int, node_id: str | None = None, node_max: int = 0,
                        class_cfg: str = '{"_idle": 0, "standard": {"r": 0, "p": 9}}') -> bool:
        ok = await self.redis.eval(CLAIM_LUA, 4, self._q(dep), self._a(dep),
                                   f"nexora:nodeactive:{node_id}" if node_id else "", f"nexora:aclass:{dep}",
                                   str(rid), max_active, time.time() + self.lease, node_max or 0, node_id or "",
                                   class_cfg, str(dep))
        return bool(ok)

    async def move_queued(self, old_dep, new_dep, rid, max_queue: int, idle_s: int = 0) -> int:
        """Returns the new sequence, -1 if the destination is full, -2 if rid was no longer queued."""
        return int(await self.redis.eval(MOVE_LUA, 3, self._q(old_dep), f"nexora:seq:{new_dep}",
                                         self._q(new_dep), str(rid), max_queue, str(old_dep), str(new_dep), idle_s))

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
        await self.redis.eval(RELEASE_LUA, 1, self._a(dep), str(rid), str(dep))

    async def remove_queued(self, dep, rid) -> bool:
        return bool(await self.redis.eval(REMOVE_LUA, 1, self._q(dep), str(rid), str(dep)))

    async def class_state(self, dep, classes) -> dict:
        """Live per-class numbers for one deployment: running, waiting and recently-seen."""
        raw = await self.redis.hgetall(f"nexora:aclass:{dep}")
        running = {(k.decode() if isinstance(k, bytes) else k): int(v) for k, v in raw.items()}
        queued, seen = {}, {}
        for c in classes:
            queued[c] = int(await self.redis.zcard(f"nexora:cq:{dep}:{c}"))
            seen[c] = bool(await self.redis.exists(f"nexora:seen:{dep}:{c}"))
        return {"active": running, "queued": queued, "seen": seen}

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
