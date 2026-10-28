package revoke

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

var t0 = time.Unix(1_790_000_000, 0)

type bed struct {
	mr    *miniredis.Miniredis
	now   atomic.Int64 // Unix seconds
	list  *List
	maxLf time.Duration
}

func (b *bed) clock() time.Time { return time.Unix(b.now.Load(), 0) }

func newBed(t *testing.T, policy Policy) *bed {
	t.Helper()
	b := &bed{mr: miniredis.RunT(t), maxLf: 168 * time.Hour}
	b.now.Store(t0.Unix())
	b.list = b.replica(t, policy)
	return b
}

// replica makes another list over the same store, as a second service replica would.
func (b *bed) replica(t *testing.T, policy Policy) *List {
	t.Helper()
	rdb := redis.NewClient(&redis.Options{Addr: b.mr.Addr()})
	t.Cleanup(func() { _ = rdb.Close() })
	return New(rdb, Config{Prefix: "authlimit:v1:", PollEvery: time.Second, MaxStale: 30 * time.Second, Policy: policy, Timeout: time.Second}, b.clock, nil, nil)
}

func (b *bed) revoke(t *testing.T, tenant, sub string, at time.Time) {
	t.Helper()
	if err := b.list.Add(context.Background(), tenant, sub, at, b.maxLf); err != nil {
		t.Fatal(err)
	}
}

func (b *bed) poll(t *testing.T, l *List) {
	t.Helper()
	if err := l.Poll(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestATokenIssuedBeforeTheRevocationIsVoidAndOneIssuedAfterWorks(t *testing.T) {
	b := newBed(t, Closed)
	b.revoke(t, "ecorp", "alice", t0)
	b.poll(t, b.list)

	cases := []struct {
		iat  int64
		want Verdict
	}{
		{t0.Unix() - 3600, Revoked}, // issued an hour before: void
		{t0.Unix(), Revoked},        // issued in the same second: void (the documented one-second edge)
		{t0.Unix() + 1, OK},         // signed in again after the revocation: works
		{0, Revoked},                // a token with no issue time: any revocation voids it
	}
	for _, tc := range cases {
		if got := b.list.Check("ecorp", "alice", tc.iat); got != tc.want {
			t.Errorf("iat %d: %v, want %v", tc.iat-t0.Unix(), got, tc.want)
		}
	}
}

func TestARevocationIsScopedToItsTenantAndPerson(t *testing.T) {
	b := newBed(t, Closed)
	b.revoke(t, "ecorp", "alice", t0)
	b.poll(t, b.list)
	for _, who := range [][2]string{{"ecorp", "bob"}, {"other-co", "alice"}, {"ecorp", "ALICE"}, {"ecorp\x00alice", ""}, {"", "alice"}} {
		if who[0] == "" || who[1] == "" {
			continue
		}
		if got := b.list.Check(who[0], who[1], t0.Unix()-10); got != OK {
			t.Errorf("%v was affected by a revocation of ecorp/alice: %v", who, got)
		}
	}
}

func TestEveryReplicaSeesARevocationWithinOnePoll(t *testing.T) {
	b := newBed(t, Closed)
	second := b.replica(t, Closed)
	b.poll(t, b.list)
	b.poll(t, second)
	if b.list.Check("ecorp", "alice", t0.Unix()-5) != OK || second.Check("ecorp", "alice", t0.Unix()-5) != OK {
		t.Fatal("nobody is revoked yet")
	}
	b.revoke(t, "ecorp", "alice", t0)
	b.poll(t, b.list)
	b.poll(t, second)
	if b.list.Check("ecorp", "alice", t0.Unix()-5) != Revoked || second.Check("ecorp", "alice", t0.Unix()-5) != Revoked {
		t.Fatal("both replicas must refuse the revoked person after one poll")
	}
}

func TestARunningListPicksUpARevocationOnItsOwn(t *testing.T) {
	// The 5-second requirement (FR-019) is the poll interval plus a margin; this runs the real loop with a short interval.
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()
	l := New(rdb, Config{Prefix: "authlimit:v1:", PollEvery: 20 * time.Millisecond, MaxStale: time.Second, Policy: Closed, Timeout: time.Second}, nil, nil, nil)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go l.Run(ctx)
	if err := l.Add(ctx, "ecorp", "alice", time.Now(), time.Hour); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if l.Check("ecorp", "alice", time.Now().Unix()-5) == Revoked {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("the running loop never picked up the revocation")
}

func TestARevokedPersonStaysRevokedThroughAStoreOutage(t *testing.T) {
	// The point of the local copy: Redis going away must not un-revoke anyone, and must not stall a request.
	b := newBed(t, Closed)
	b.revoke(t, "ecorp", "alice", t0)
	b.poll(t, b.list)
	b.mr.Close()
	b.now.Add(10) // inside the 30 s ceiling
	if err := b.list.Poll(context.Background()); err == nil {
		t.Fatal("polling a dead store must fail")
	}
	if got := b.list.Check("ecorp", "alice", t0.Unix()-5); got != Revoked {
		t.Fatalf("alice is %v during the outage, want Revoked", got)
	}
	if got := b.list.Check("ecorp", "bob", t0.Unix()-5); got != OK {
		t.Fatalf("bob is %v during the outage inside the ceiling, want OK", got)
	}
}

func TestPastTheCeilingTheConfiguredPolicyApplies(t *testing.T) {
	for _, tc := range []struct {
		policy Policy
		want   Verdict
	}{{Closed, Unavailable}, {Open, OK}} {
		b := newBed(t, tc.policy)
		b.poll(t, b.list)
		b.mr.Close()
		b.now.Add(31) // past the 30 s ceiling
		if got := b.list.Check("ecorp", "bob", t0.Unix()); got != tc.want {
			t.Errorf("policy %s: %v, want %v", tc.policy, got, tc.want)
		}
		if tc.policy == Open {
			// Open still enforces the copy it has.
			b2 := newBed(t, Open)
			b2.revoke(t, "ecorp", "alice", t0)
			b2.poll(t, b2.list)
			b2.mr.Close()
			b2.now.Add(3600)
			if got := b2.list.Check("ecorp", "alice", t0.Unix()-5); got != Revoked {
				t.Errorf("policy open must keep enforcing its last copy, got %v", got)
			}
		}
	}
}

func TestAReplicaThatNeverLoadedACopy(t *testing.T) {
	for policy, want := range map[Policy]Verdict{Closed: Unavailable, Open: OK} {
		b := newBed(t, policy)
		if got := b.list.Check("ecorp", "bob", t0.Unix()); got != want {
			t.Errorf("policy %s with no copy: %v, want %v", policy, got, want)
		}
	}
	b := newBed(t, Closed)
	if b.list.Fresh() || b.list.Usable() {
		t.Error("a replica with no copy is neither fresh nor usable under policy closed")
	}
	if !newBed(t, Open).list.Usable() {
		t.Error("under policy open a replica is usable without a copy")
	}
}

func TestRecoveryAfterAnOutage(t *testing.T) {
	b := newBed(t, Closed)
	b.poll(t, b.list)
	b.now.Add(31)
	if b.list.Check("ecorp", "bob", t0.Unix()) != Unavailable {
		t.Fatal("stale under closed must be unavailable")
	}
	b.poll(t, b.list) // the store was never down in this test: a successful poll refreshes the copy
	if b.list.Check("ecorp", "bob", t0.Unix()) != OK {
		t.Fatal("a fresh poll must restore service")
	}
}

func TestAddTrimsRecordsNoTokenCouldStillMatch(t *testing.T) {
	b := newBed(t, Closed)
	old := t0.Add(-200 * time.Hour) // older than the 168 h cap
	b.revoke(t, "ecorp", "long-gone", old)
	b.revoke(t, "ecorp", "alice", t0) // trims anything older than t0 - 168h
	entries, err := b.list.Entries(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 || entries[0].Principal != "alice" {
		t.Fatalf("entries = %+v, want only alice", entries)
	}
}

func TestARepeatedRevocationMovesTheCutOffForward(t *testing.T) {
	b := newBed(t, Closed)
	b.revoke(t, "ecorp", "alice", t0)
	b.revoke(t, "ecorp", "alice", t0.Add(time.Hour))
	b.poll(t, b.list)
	if got := b.list.Check("ecorp", "alice", t0.Unix()+1800); got != Revoked {
		t.Fatalf("a token issued between the two revocations: %v, want Revoked", got)
	}
	entries, _ := b.list.Entries(context.Background())
	if len(entries) != 1 {
		t.Fatalf("one person, one record: %+v", entries)
	}
}

func TestAddRefusesAnEmptyIdentity(t *testing.T) {
	b := newBed(t, Closed)
	if err := b.list.Add(context.Background(), "", "alice", t0, time.Hour); err == nil {
		t.Error("empty tenant accepted")
	}
	if err := b.list.Add(context.Background(), "ecorp", "", t0, time.Hour); err == nil {
		t.Error("empty principal accepted: it would be a cut-off for nobody and hide the typo")
	}
}

func TestEntriesRoundTripTenantAndPerson(t *testing.T) {
	b := newBed(t, Closed)
	b.revoke(t, "ecorp", "alice", t0)
	b.revoke(t, "other-co", "bob", t0.Add(time.Minute))
	entries, err := b.list.Entries(context.Background())
	if err != nil || len(entries) != 2 || entries[0].Tenant != "ecorp" || entries[1].Principal != "bob" {
		t.Fatalf("%+v %v", entries, err)
	}
}
