-- One atomic decision over TWO token buckets: the tenant's and the person's.
--
-- KEYS[1]  tenant bucket       KEYS[2]  person bucket
-- ARGV[1]  tenant capacity     ARGV[2]  tenant refill      (capacity in ticks; refill in ticks per millisecond)
-- ARGV[3]  person capacity     ARGV[4]  person refill
-- ARGV[5]  cost of one request (ticks)
-- ARGV[6]  tenant key TTL ms   ARGV[7]  person key TTL ms
-- ARGV[8]  "now" in ms to use instead of Redis TIME (0 = use TIME); only tests pass it
--
-- Units. One token is 60000 ticks, so a refill of N tokens per minute is exactly N ticks per millisecond: every number here is a whole
-- number and nothing drifts. (Fractions of a token per call would be lost to rounding when calls are frequent, so a busy bucket would
-- refill slower than configured.)
--
-- Why both buckets in one call: a request is admitted only if BOTH have a token, and it must then take one from each. Two separate calls
-- could take the tenant's token and then be refused by the person's, charging the tenant for a request that was never served.
--
-- Why Redis TIME and not the caller's clock: replicas must not each decide how much time has passed. One clock, the store's.
--
-- Returns {allowed (1/0), wait in ms until the emptier bucket has a token (0 when allowed)}.

local now = tonumber(ARGV[8])
if now == nil or now <= 0 then
  local t = redis.call('TIME')
  now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
end
local cost = tonumber(ARGV[5])

local function refilled(key, capacity, rate)
  local h = redis.call('HMGET', key, 'tk', 'ts')
  local tk, ts = tonumber(h[1]), tonumber(h[2])
  if tk == nil or ts == nil then
    return capacity -- a bucket nobody has used starts full
  end
  local elapsed = now - ts
  if elapsed < 0 then elapsed = 0 end -- the store's clock stepped back: add nothing rather than subtract
  tk = tk + elapsed * rate
  if tk > capacity then tk = capacity end
  return tk
end

local tcap, trate = tonumber(ARGV[1]), tonumber(ARGV[2])
local pcap, prate = tonumber(ARGV[3]), tonumber(ARGV[4])
local ttk = refilled(KEYS[1], tcap, trate)
local ptk = refilled(KEYS[2], pcap, prate)

local allowed = ttk >= cost and ptk >= cost
local wait = 0
if allowed then
  ttk = ttk - cost
  ptk = ptk - cost
else
  -- Nothing is consumed, but the refilled amounts are stored below so the elapsed time is not lost.
  if ttk < cost then wait = math.max(wait, math.ceil((cost - ttk) / trate)) end
  if ptk < cost then wait = math.max(wait, math.ceil((cost - ptk) / prate)) end
end

redis.call('HSET', KEYS[1], 'tk', ttk, 'ts', now)
redis.call('PEXPIRE', KEYS[1], ARGV[6])
redis.call('HSET', KEYS[2], 'tk', ptk, 'ts', now)
redis.call('PEXPIRE', KEYS[2], ARGV[7])

if allowed then
  return {1, 0}
end
return {0, wait}
