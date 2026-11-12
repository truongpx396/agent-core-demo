package limiter

import (
	"context"
	_ "embed"
	"errors"
	"fmt"
	"math"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

//go:embed bucket.lua
var bucketScript string

const ticksPerToken = 60000 // see bucket.lua: a refill of N per minute is N ticks per millisecond

// Decision is the limiter's answer for one request.
type Decision struct {
	Allowed bool
	// RetryAfter is how long until the emptier bucket has a token, rounded UP to whole seconds, at least one second (an HTTP Retry-After
	// of 0 invites an immediate retry). Zero when Allowed.
	RetryAfter time.Duration
}

// Limiter charges a request to the tenant's and the person's buckets, or, for a request that carries no identity, to its address's.
type Limiter interface {
	Take(ctx context.Context, tenant, person string, class Class, lim Limits) (Decision, error)
	TakeAddress(ctx context.Context, name, address string, b Bucket) (Decision, error)
}

// Redis is the Redis-backed limiter. Every call has a deadline; on an error or a timeout it returns the error and the CALLER serves the
// request (the limiter fails open: it is a protection, and a store outage must not become an API outage). The caller counts the failure.
type Redis struct {
	rdb     redis.Scripter
	prefix  string
	timeout time.Duration
	script  *redis.Script
	nowMS   func() int64 // nil: the script uses Redis TIME. Tests inject a clock.
}

// NewRedis builds the limiter. `prefix` is AUTHLIMIT_KEY_PREFIX (default "authlimit:v1:"), so its keys can never collide with the work
// queue's. `nowMS` may be nil.
func NewRedis(rdb redis.Scripter, prefix string, timeout time.Duration, nowMS func() int64) *Redis {
	return &Redis{rdb: rdb, prefix: prefix, timeout: timeout, script: redis.NewScript(bucketScript), nowMS: nowMS}
}

// escape makes a tenant or person id safe inside a key: the separators and the hash-tag braces are percent-encoded, so one id can never
// be spelled to land in another's key.
var escape = strings.NewReplacer("%", "%25", ":", "%3A", "{", "%7B", "}", "%7D")

func (r *Redis) keys(tenant, person string, class Class) (string, string) {
	t := escape.Replace(tenant)
	p := escape.Replace(person)
	// The braces are a hash tag: both keys of one request share a slot if the store is ever clustered.
	return fmt.Sprintf("%sb:{%s}:t:%s", r.prefix, t, class), fmt.Sprintf("%sb:{%s}:p:%s:%s", r.prefix, t, p, class)
}

// Take implements Limiter.
func (r *Redis) Take(ctx context.Context, tenant, person string, class Class, lim Limits) (Decision, error) {
	ctx, cancel := context.WithTimeout(ctx, r.timeout)
	defer cancel()
	tKey, pKey := r.keys(tenant, person, class)
	tCap, tRate := int64(lim.Tenant.Capacity)*ticksPerToken, int64(lim.Tenant.PerMinute)
	pCap, pRate := int64(lim.Person.Capacity)*ticksPerToken, int64(lim.Person.PerMinute)
	var now int64
	if r.nowMS != nil {
		now = r.nowMS()
	}
	res, err := r.script.Run(ctx, r.rdb, []string{tKey, pKey},
		tCap, tRate, pCap, pRate, ticksPerToken, ttlMillis(tCap, tRate), ttlMillis(pCap, pRate), now).Int64Slice()
	if err != nil {
		return Decision{}, err
	}
	if len(res) != 2 {
		return Decision{}, errors.New("limiter: unexpected script reply")
	}
	if res[0] == 1 {
		return Decision{Allowed: true}, nil
	}
	return Decision{RetryAfter: retryAfter(res[1])}, nil
}

// TakeAddress charges one request to the bucket of a named address limit (routes.json "address_limits") for one network address.
//
// It runs the same script as Take with ONE bucket named twice: both of the script's views read and write the same hash, so they always
// agree and the one token is taken once. Reusing the script keeps a single copy of the refill arithmetic, which is the part that was
// audited and mutation-tested, rather than a second one to keep in step.
func (r *Redis) TakeAddress(ctx context.Context, name, address string, b Bucket) (Decision, error) {
	ctx, cancel := context.WithTimeout(ctx, r.timeout)
	defer cancel()
	// `a:` cannot collide with the `b:` keys of the tenant and person buckets. The address is escaped like an id: an IPv6 address is
	// full of the separator.
	key := fmt.Sprintf("%sa:%s:%s", r.prefix, escape.Replace(name), escape.Replace(address))
	capTicks, rate := int64(b.Capacity)*ticksPerToken, int64(b.PerMinute)
	var now int64
	if r.nowMS != nil {
		now = r.nowMS()
	}
	ttl := ttlMillis(capTicks, rate)
	res, err := r.script.Run(ctx, r.rdb, []string{key, key}, capTicks, rate, capTicks, rate, ticksPerToken, ttl, ttl, now).Int64Slice()
	if err != nil {
		return Decision{}, err
	}
	if len(res) != 2 {
		return Decision{}, errors.New("limiter: unexpected script reply")
	}
	if res[0] == 1 {
		return Decision{Allowed: true}, nil
	}
	return Decision{RetryAfter: retryAfter(res[1])}, nil
}

// ttlMillis is twice the time to fill an empty bucket, at least a second: long enough that a bucket in use never expires mid-refill,
// short enough that a person who left costs no memory for long.
func ttlMillis(capTicks, ratePerMs int64) int64 {
	fill := capTicks / ratePerMs
	if ttl := 2 * fill; ttl > 1000 {
		return ttl
	}
	return 1000
}

func retryAfter(waitMs int64) time.Duration {
	seconds := int64(math.Ceil(float64(waitMs) / 1000))
	if seconds < 1 {
		seconds = 1
	}
	return time.Duration(seconds) * time.Second
}
