// Package revoke is the revocation list: "tokens issued to this person before moment T are void".
//
// Why it exists. Access tokens last seven days by default and are verified offline, without asking the authorization server, so a stolen
// or abandoned token would otherwise stay good for a week (threat T9). Revocation is how an operator bounds that.
//
// The shape of it, and the reasons:
//
//   - It is "issued at or before T", not a list of token ids. Casdoor's tokens are not guaranteed to carry a `jti`, and a per-person
//     cut-off needs no id: after T the person signs in again and the new token (issued after T) works.
//   - Every replica COPIES the whole list into memory every second, and a request is a map lookup. The alternative, a Redis call per
//     request, would make the store's health part of every request's latency and would force ONE failure policy on two different things:
//     the limiter (a protection: fail open) and revocation (a security check: fail closed). With a local copy they are independent, and
//     a Redis blip does not stall anything.
//   - If the copy cannot be refreshed the replica keeps enforcing the last copy. Only when the copy is older than a ceiling does the
//     configured policy apply: refuse (closed, the default) or serve (open). Principle V: no unbounded wait, no silent degradation.
//   - The list is small by nature (people revoked in the last token lifetime). It is bounded at 10,000 entries and exceeding that is
//     logged, not truncated: dropping a revocation silently would be the worst failure this package could have.
package revoke

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sort"
	"strconv"
	"sync"
	"time"

	"github.com/redis/go-redis/v9"

	"authlimit/internal/telemetry"
)

// MaxEntries is the documented size bound of the list.
const MaxEntries = 10000

// Policy is what to do when the local copy is too old to trust.
type Policy string

const (
	Closed Policy = "closed" // refuse: a security check that cannot be made is not passed
	Open   Policy = "open"   // serve with the last copy
)

// Verdict is the answer for one token.
type Verdict int

const (
	OK Verdict = iota
	Revoked
	// Unavailable: the copy is too old and the policy is Closed.
	Unavailable
)

// Entry is one revocation.
type Entry struct {
	Tenant    string
	Principal string
	At        time.Time
}

func member(tenant, principal string) string { return tenant + "\x00" + principal }

// List is the revocation list: the Redis sorted set and this replica's copy of it.
type List struct {
	rdb       redis.Cmdable
	key       string
	pollEvery time.Duration
	maxStale  time.Duration
	policy    Policy
	timeout   time.Duration
	now       func() time.Time
	metrics   *telemetry.Metrics
	log       *slog.Logger

	mu       sync.RWMutex
	entries  map[string]int64 // member -> revoked_at (Unix seconds)
	loadedAt time.Time
	warned   bool
}

// Config groups the list's settings.
type Config struct {
	Prefix    string        // AUTHLIMIT_KEY_PREFIX
	PollEvery time.Duration // AUTHLIMIT_REVOCATION_POLL_MS
	MaxStale  time.Duration // AUTHLIMIT_REVOCATION_MAX_STALENESS_SECONDS
	Policy    Policy        // AUTHLIMIT_REVOCATION_UNAVAILABLE
	Timeout   time.Duration // AUTHLIMIT_REDIS_TIMEOUT_MS
}

// New builds a list. `now`, `m` and `log` may be nil.
func New(rdb redis.Cmdable, cfg Config, now func() time.Time, m *telemetry.Metrics, log *slog.Logger) *List {
	if now == nil {
		now = time.Now
	}
	if log == nil {
		log = slog.New(slog.DiscardHandler)
	}
	return &List{
		rdb: rdb, key: cfg.Prefix + "revoked", pollEvery: cfg.PollEvery, maxStale: cfg.MaxStale, policy: cfg.Policy,
		timeout: cfg.Timeout, now: now, metrics: m, log: log, entries: map[string]int64{},
	}
}

// Poll loads the whole list. On failure the previous copy stays in force (and ages).
func (l *List) Poll(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, l.timeout)
	defer cancel()
	zs, err := l.rdb.ZRangeWithScores(ctx, l.key, 0, -1).Result()
	if err != nil {
		l.metrics.StoreError(telemetry.StoreRevocation)
		l.log.Warn("revocation_poll_failed", "error", err.Error())
		return err
	}
	next := make(map[string]int64, len(zs))
	for _, z := range zs {
		m, ok := z.Member.(string)
		if !ok {
			continue
		}
		next[m] = int64(z.Score)
	}
	l.mu.Lock()
	l.entries, l.loadedAt = next, l.now()
	tooMany := len(next) > MaxEntries && !l.warned
	if tooMany {
		l.warned = true
	}
	l.mu.Unlock()
	if tooMany {
		l.log.Warn("revocation_list_over_bound", "entries", len(next), "bound", MaxEntries)
	}
	return nil
}

// Age is how old this replica's copy is (a replica that never loaded one reports its age as unbounded via fresh=false).
func (l *List) Age() (age time.Duration, loaded bool) {
	l.mu.RLock()
	defer l.mu.RUnlock()
	if l.loadedAt.IsZero() {
		return 0, false
	}
	return l.now().Sub(l.loadedAt), true
}

// Fresh reports whether the copy is young enough to trust.
func (l *List) Fresh() bool {
	age, loaded := l.Age()
	return loaded && age <= l.maxStale
}

// Usable is whether this replica can answer revocation questions under its policy: a fresh copy, or any copy at all under Open.
func (l *List) Usable() bool {
	if l.Fresh() {
		return true
	}
	return l.policy == Open
}

// Check answers for one token. `iat` is the token's issue time in Unix seconds (0 when the token has none: any revocation of the person
// then voids it, the safe direction).
func (l *List) Check(tenant, principal string, iat int64) Verdict {
	if !l.Fresh() && l.policy == Closed {
		return Unavailable
	}
	l.mu.RLock()
	at, ok := l.entries[member(tenant, principal)]
	l.mu.RUnlock()
	if ok && iat <= at {
		return Revoked
	}
	return OK
}

// Tick updates the age gauge. Run calls it every poll; tests call it directly.
func (l *List) Tick() {
	if age, loaded := l.Age(); loaded {
		l.metrics.SetRevocationAge(age)
	} else {
		// Never loaded: report time since start as unbounded growth is impossible here, so report the staleness ceiling plus one second:
		// above the alert threshold, finite, and honest that no copy exists.
		l.metrics.SetRevocationAge(l.maxStale + time.Second)
	}
}

// Run polls until ctx ends.
func (l *List) Run(ctx context.Context) {
	_ = l.Poll(ctx)
	l.Tick()
	t := time.NewTicker(l.pollEvery)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			_ = l.Poll(ctx)
			l.Tick()
		}
	}
}

// Add revokes a person as of `at`: every token issued to them at or before it is void. It also trims entries older than `maxLife` (no
// token that old can still be valid, so the record has nothing left to do). It is what `authlimit revoke` calls.
func (l *List) Add(ctx context.Context, tenant, principal string, at time.Time, maxLife time.Duration) error {
	if tenant == "" || principal == "" {
		return errors.New("revoke: tenant and principal are both required")
	}
	ctx, cancel := context.WithTimeout(ctx, l.timeout)
	defer cancel()
	pipe := l.rdb.TxPipeline()
	pipe.ZAdd(ctx, l.key, redis.Z{Score: float64(at.Unix()), Member: member(tenant, principal)})
	pipe.ZRemRangeByScore(ctx, l.key, "-inf", "("+strconv.FormatInt(at.Add(-maxLife).Unix(), 10))
	if _, err := pipe.Exec(ctx); err != nil {
		return fmt.Errorf("revoke: %w", err)
	}
	return nil
}

// Entries lists the current revocations from the store (not this replica's copy), oldest first.
func (l *List) Entries(ctx context.Context) ([]Entry, error) {
	ctx, cancel := context.WithTimeout(ctx, l.timeout)
	defer cancel()
	zs, err := l.rdb.ZRangeWithScores(ctx, l.key, 0, -1).Result()
	if err != nil {
		return nil, err
	}
	out := make([]Entry, 0, len(zs))
	for _, z := range zs {
		m, _ := z.Member.(string)
		tenant, principal, ok := splitMember(m)
		if !ok {
			continue
		}
		out = append(out, Entry{Tenant: tenant, Principal: principal, At: time.Unix(int64(z.Score), 0)})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].At.Before(out[j].At) })
	return out, nil
}

func splitMember(m string) (tenant, principal string, ok bool) {
	for i := 0; i < len(m); i++ {
		if m[i] == 0 {
			return m[:i], m[i+1:], true
		}
	}
	return "", "", false
}
