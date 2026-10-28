package limiter

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

// testbed is a limiter over an in-memory Redis with a clock the test moves by hand.
type testbed struct {
	mr  *miniredis.Miniredis
	rdb *redis.Client
	now atomic.Int64
	lim *Redis
}

func newTestbed(t *testing.T) *testbed {
	t.Helper()
	mr := miniredis.RunT(t)
	tb := &testbed{mr: mr, rdb: redis.NewClient(&redis.Options{Addr: mr.Addr()})}
	t.Cleanup(func() { _ = tb.rdb.Close() })
	tb.now.Store(1_790_000_000_000)
	tb.lim = NewRedis(tb.rdb, "authlimit:v1:", time.Second, tb.now.Load)
	return tb
}

func (tb *testbed) advance(d time.Duration) { tb.now.Add(d.Milliseconds()) }

func (tb *testbed) take(t *testing.T, tenant, person string, lim Limits) Decision {
	t.Helper()
	d, err := tb.lim.Take(context.Background(), tenant, person, Turn, lim)
	if err != nil {
		t.Fatal(err)
	}
	return d
}

// limits builds a Limits with tenant {tc, tpm} and person {pc, ppm}.
func limits(tc, tpm, pc, ppm int) Limits {
	return Limits{Tenant: Bucket{Capacity: tc, PerMinute: tpm}, Person: Bucket{Capacity: pc, PerMinute: ppm}}
}

func TestABucketStartsFullAndRunsOut(t *testing.T) {
	tb := newTestbed(t)
	l := limits(10, 30, 3, 60)
	for i := 1; i <= 3; i++ {
		if d := tb.take(t, "ecorp", "alice", l); !d.Allowed {
			t.Fatalf("request %d of a capacity-3 person bucket was refused", i)
		}
	}
	d := tb.take(t, "ecorp", "alice", l)
	if d.Allowed {
		t.Fatal("the 4th request must be refused")
	}
	if d.RetryAfter != time.Second {
		t.Fatalf("RetryAfter = %s; the person refills 60 per minute (1 per second), so one second", d.RetryAfter)
	}
}

func TestItRefillsAtTheConfiguredRate(t *testing.T) {
	tb := newTestbed(t)
	l := limits(10, 600, 2, 60) // person: 2 tokens, 1 per second
	tb.take(t, "ecorp", "alice", l)
	tb.take(t, "ecorp", "alice", l)
	if tb.take(t, "ecorp", "alice", l).Allowed {
		t.Fatal("empty")
	}
	tb.advance(1000 * time.Millisecond)
	if !tb.take(t, "ecorp", "alice", l).Allowed {
		t.Fatal("one second later one token must be back")
	}
	if tb.take(t, "ecorp", "alice", l).Allowed {
		t.Fatal("and only one")
	}
}

func TestRefillDoesNotDriftWhenCallsAreFrequent(t *testing.T) {
	// The reason the script counts in ticks: with 10 tokens a minute, a call every 100 ms must still refill one token per 6 seconds in
	// total. Rounding the refill per call would lose the fraction each time and the bucket would refill several times slower.
	tb := newTestbed(t)
	l := limits(100, 100000, 1, 10) // person: capacity 1, 10 per minute
	tb.take(t, "ecorp", "alice", l) // empty it
	allowedAt := time.Duration(-1)
	for elapsed := time.Duration(0); elapsed <= 7*time.Second; elapsed += 100 * time.Millisecond {
		tb.advance(100 * time.Millisecond)
		if tb.take(t, "ecorp", "alice", l).Allowed {
			allowedAt = elapsed + 100*time.Millisecond
			break
		}
	}
	if allowedAt < 5900*time.Millisecond || allowedAt > 6200*time.Millisecond {
		t.Fatalf("the token came back after %s, want about 6s (10 per minute)", allowedAt)
	}
}

func TestACapIsNeverExceededByIdling(t *testing.T) {
	tb := newTestbed(t)
	l := limits(100, 6000, 3, 60)
	tb.advance(24 * time.Hour)
	granted := 0
	for range 10 {
		if tb.take(t, "ecorp", "alice", l).Allowed {
			granted++
		}
	}
	if granted != 3 {
		t.Fatalf("a day of idleness granted a burst of %d, want the capacity, 3", granted)
	}
}

func TestADeniedPersonDoesNotChargeTheTenant(t *testing.T) {
	// Both buckets in one atomic call: a request refused by the person's bucket must not have taken the tenant's token.
	tb := newTestbed(t)
	l := limits(5, 60, 1, 60)
	tb.take(t, "ecorp", "alice", l) // alice's only token; the tenant has 4 left
	for range 20 {
		if tb.take(t, "ecorp", "alice", l).Allowed {
			t.Fatal("alice is out of tokens")
		}
	}
	tKey, _ := tb.lim.keys("ecorp", "alice", Turn)
	got, err := tb.rdb.HGet(context.Background(), tKey, "tk").Int64()
	if err != nil {
		t.Fatal(err)
	}
	if want := int64(4 * ticksPerToken); got != want {
		t.Fatalf("the tenant bucket holds %d ticks, want %d: refused requests charged it", got, want)
	}
	// And a colleague is not affected by alice's refusals.
	if !tb.take(t, "ecorp", "bob", l).Allowed {
		t.Fatal("bob must be served")
	}
}

func TestADeniedTenantDoesNotChargeThePerson(t *testing.T) {
	tb := newTestbed(t)
	l := limits(1, 60, 5, 60)
	tb.take(t, "ecorp", "alice", l) // the tenant's only token
	if tb.take(t, "ecorp", "bob", l).Allowed {
		t.Fatal("the tenant is out of tokens")
	}
	_, pKey := tb.lim.keys("ecorp", "bob", Turn)
	got, _ := tb.rdb.HGet(context.Background(), pKey, "tk").Int64()
	if want := int64(5 * ticksPerToken); got != want {
		t.Fatalf("bob's bucket holds %d ticks, want %d", got, want)
	}
}

func TestTenantsAreIndependent(t *testing.T) {
	tb := newTestbed(t)
	l := limits(1, 60, 5, 60)
	tb.take(t, "ecorp", "alice", l)
	if tb.take(t, "ecorp", "alice", l).Allowed {
		t.Fatal("ecorp is out")
	}
	if !tb.take(t, "other-co", "alice", l).Allowed {
		t.Fatal("another tenant (even with a person of the same name) must not share ecorp's buckets")
	}
}

func TestClassesHaveSeparateBuckets(t *testing.T) {
	tb := newTestbed(t)
	l := limits(1, 60, 1, 60)
	if d, err := tb.lim.Take(context.Background(), "ecorp", "alice", Turn, l); err != nil || !d.Allowed {
		t.Fatal("turn")
	}
	if d, err := tb.lim.Take(context.Background(), "ecorp", "alice", Upload, l); err != nil || !d.Allowed {
		t.Fatal("an upload must not be limited by the turn bucket")
	}
}

func TestTwoReplicasCountOnce(t *testing.T) {
	// The limit is shared: two limiters over one store behave as one.
	tb := newTestbed(t)
	other := NewRedis(redis.NewClient(&redis.Options{Addr: tb.mr.Addr()}), "authlimit:v1:", time.Second, tb.now.Load)
	l := limits(100, 6000, 4, 60)
	granted := 0
	for i := range 10 {
		var lim *Redis = tb.lim
		if i%2 == 1 {
			lim = other
		}
		d, err := lim.Take(context.Background(), "ecorp", "alice", Turn, l)
		if err != nil {
			t.Fatal(err)
		}
		if d.Allowed {
			granted++
		}
	}
	if granted != 4 {
		t.Fatalf("two replicas granted %d, want exactly the capacity, 4", granted)
	}
}

func TestRetryAfterIsTheLongerWaitWhenBothAreEmpty(t *testing.T) {
	tb := newTestbed(t)
	l := limits(1, 30, 1, 10) // tenant refills a token in 2 s, person in 6 s
	tb.take(t, "ecorp", "alice", l)
	if d := tb.take(t, "ecorp", "alice", l); d.Allowed || d.RetryAfter != 6*time.Second {
		t.Fatalf("decision = %+v, want a 6 s wait (the slower bucket)", d)
	}
	tb.advance(3 * time.Second) // the tenant has refilled; only the person is still empty
	if d := tb.take(t, "ecorp", "alice", l); d.Allowed || d.RetryAfter != 3*time.Second {
		t.Fatalf("decision = %+v, want the 3 s that remain", d)
	}
}

func TestRetryAfterIsAtLeastOneSecond(t *testing.T) {
	tb := newTestbed(t)
	l := limits(1, 6000, 1, 6000) // a token every 10 ms
	tb.take(t, "ecorp", "alice", l)
	if d := tb.take(t, "ecorp", "alice", l); d.Allowed || d.RetryAfter != time.Second {
		t.Fatalf("decision = %+v: a Retry-After of 0 invites an immediate retry", d)
	}
}

func TestBucketsExpireOnTheirOwn(t *testing.T) {
	tb := newTestbed(t)
	l := limits(10, 30, 5, 10) // person fills from empty in 30 s, so the key lives 60 s
	tb.take(t, "ecorp", "alice", l)
	tKey, pKey := tb.lim.keys("ecorp", "alice", Turn)
	if ttl := tb.mr.TTL(pKey); ttl <= 0 || ttl > 60*time.Second {
		t.Fatalf("person key TTL = %s", ttl)
	}
	if ttl := tb.mr.TTL(tKey); ttl <= 0 || ttl > 40*time.Second {
		t.Fatalf("tenant key TTL = %s", ttl) // 10 tokens at 30/min fill in 20 s
	}
}

func TestKeysCannotBeSpelledToCollide(t *testing.T) {
	// Tenants and persons come from the identity provider. Whatever they contain, two different (tenant, person) pairs must map to two
	// different keys, or one person could land in another's bucket. The tenant bucket is the one key deliberately shared (by every
	// person of ONE tenant); the person bucket must belong to exactly one (tenant, person).
	tb := newTestbed(t)
	alphabet := []string{"a", ":", "{", "}", "%", "t", "p"}
	var ids []string
	var build func(prefix string, depth int)
	build = func(prefix string, depth int) {
		if prefix != "" {
			ids = append(ids, prefix)
		}
		if depth == 0 {
			return
		}
		for _, c := range alphabet {
			build(prefix+c, depth-1)
		}
	}
	build("", 3)
	type owner struct{ tenant, person string }
	tenantKeys := map[string]string{} // key -> the one tenant that may own it
	personKeys := map[string]owner{}
	checked := 0
	for _, tenant := range ids {
		for _, person := range ids {
			tk, pk := tb.lim.keys(tenant, person, Turn)
			if prev, ok := tenantKeys[tk]; ok && prev != tenant {
				t.Fatalf("tenant bucket %q is produced by tenants %q and %q", tk, prev, tenant)
			}
			tenantKeys[tk] = tenant
			if prev, ok := personKeys[pk]; ok && prev != (owner{tenant, person}) {
				t.Fatalf("person bucket %q is produced by %+v and %+v", pk, prev, owner{tenant, person})
			}
			personKeys[pk] = owner{tenant, person}
			if tk == pk {
				t.Fatalf("the tenant and person buckets of %q/%q are the same key", tenant, person)
			}
			checked++
		}
	}
	if checked < 100000 || len(tenantKeys) != len(ids) {
		t.Fatalf("the check did not run over the whole space: %d pairs, %d distinct tenant keys for %d tenants", checked, len(tenantKeys), len(ids))
	}
}

func TestWithoutAClockOverrideTheScriptUsesRedisTime(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()
	lim := NewRedis(rdb, "authlimit:v1:", time.Second, nil)
	l := limits(10, 600, 1, 60)
	mr.SetTime(time.Unix(1_790_000_000, 0))
	take := func() bool {
		d, err := lim.Take(context.Background(), "ecorp", "alice", Turn, l)
		if err != nil {
			t.Fatalf("the script must run with Redis TIME: %v", err)
		}
		return d.Allowed
	}
	if !take() || take() {
		t.Fatal("capacity 1")
	}
	mr.SetTime(time.Unix(1_790_000_001, 0))
	if !take() {
		t.Fatal("a second later the token must be back, by Redis's clock")
	}
}

func TestAClockThatStepsBackAddsNothingAndBreaksNothing(t *testing.T) {
	tb := newTestbed(t)
	l := limits(10, 600, 1, 60)
	tb.take(t, "ecorp", "alice", l)
	tb.advance(-time.Hour)
	if tb.take(t, "ecorp", "alice", l).Allowed {
		t.Fatal("stepping the clock back must not grant tokens")
	}
}

// --- failure: the limiter reports it, the caller fails open ----------------------------------------------------------------------

func TestAnUnreachableStoreIsAnError(t *testing.T) {
	tb := newTestbed(t)
	tb.mr.Close()
	if _, err := tb.lim.Take(context.Background(), "ecorp", "alice", Turn, limits(1, 1, 1, 1)); err == nil {
		t.Fatal("a dead store must be an error the caller can count and fail open on")
	}
}

// hangingScripter blocks every call until its context ends, like a Redis that accepted the connection and then stalled.
type hangingScripter struct{ calls atomic.Int64 }

func (h *hangingScripter) block(ctx context.Context) *redis.Cmd {
	h.calls.Add(1)
	<-ctx.Done()
	cmd := redis.NewCmd(ctx)
	cmd.SetErr(ctx.Err())
	return cmd
}
func (h *hangingScripter) Eval(ctx context.Context, _ string, _ []string, _ ...interface{}) *redis.Cmd {
	return h.block(ctx)
}
func (h *hangingScripter) EvalSha(ctx context.Context, _ string, _ []string, _ ...interface{}) *redis.Cmd {
	return h.block(ctx)
}
func (h *hangingScripter) EvalRO(ctx context.Context, _ string, _ []string, _ ...interface{}) *redis.Cmd {
	return h.block(ctx)
}
func (h *hangingScripter) EvalShaRO(ctx context.Context, _ string, _ []string, _ ...interface{}) *redis.Cmd {
	return h.block(ctx)
}
func (h *hangingScripter) ScriptExists(ctx context.Context, _ ...string) *redis.BoolSliceCmd {
	cmd := redis.NewBoolSliceCmd(ctx)
	cmd.SetErr(ctx.Err())
	return cmd
}
func (h *hangingScripter) ScriptLoad(ctx context.Context, _ string) *redis.StringCmd {
	cmd := redis.NewStringCmd(ctx)
	cmd.SetErr(ctx.Err())
	return cmd
}

func TestAStalledStoreCostsOnlyTheCeiling(t *testing.T) {
	// Principle V: no wait without a ceiling. A stalled Redis must cost a request the call timeout, not its whole life.
	lim := NewRedis(&hangingScripter{}, "authlimit:v1:", 80*time.Millisecond, nil)
	start := time.Now()
	_, err := lim.Take(context.Background(), "ecorp", "alice", Turn, limits(1, 1, 1, 1))
	if err == nil {
		t.Fatal("a stalled store must be an error")
	}
	if took := time.Since(start); took > 2*time.Second {
		t.Fatalf("the call took %s; the ceiling is 80ms", took)
	}
}

func TestRetryAfterRounding(t *testing.T) {
	for ms, want := range map[int64]time.Duration{0: time.Second, 1: time.Second, 999: time.Second, 1000: time.Second, 1001: 2 * time.Second, 6000: 6 * time.Second} {
		if got := retryAfter(ms); got != want {
			t.Errorf("retryAfter(%d) = %s, want %s", ms, got, want)
		}
	}
}
