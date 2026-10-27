package identity

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"
)

var t0 = time.Unix(1_790_000_000, 0)

// clock is a settable clock for a Keyset under test.
type clock struct{ t time.Time }

func (c *clock) now() time.Time { return c.t }

func newKeyset(t *testing.T) (string, *clock, *Keyset) {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "keys")
	c := &clock{t: t0}
	if _, err := InitKeyset(dir, t0); err != nil {
		t.Fatal(err)
	}
	ks, err := LoadKeyset(dir, false, c.now)
	if err != nil {
		t.Fatal(err)
	}
	return dir, c, ks
}

func TestInitCreatesAPrivateDirectoryAndOneCurrentKey(t *testing.T) {
	dir, _, ks := newKeyset(t)
	info, err := os.Stat(dir)
	if err != nil || info.Mode().Perm() != 0o700 {
		t.Fatalf("directory mode = %v (%v), want 0700", info.Mode().Perm(), err)
	}
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		fi, _ := e.Info()
		if fi.Mode().Perm()&0o077 != 0 {
			t.Errorf("%s is accessible to group or others (%04o)", e.Name(), fi.Mode().Perm())
		}
	}
	if st := ks.Statuses(); len(st) != 1 || st[0].State != Current {
		t.Fatalf("statuses = %+v", st)
	}
	if _, err := InitKeyset(dir, t0); err == nil {
		t.Fatal("init over an existing keyset must refuse: replacing the signing key silently hides that it happened")
	}
}

func TestLoadRefusesWhatItCannotTrust(t *testing.T) {
	write := func(dir string, m manifest) {
		raw, _ := json.Marshal(m)
		if err := os.WriteFile(filepath.Join(dir, manifestName), raw, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	t.Run("a key file readable by others", func(t *testing.T) {
		dir, c, _ := newKeyset(t)
		files, _ := filepath.Glob(filepath.Join(dir, "*.pem"))
		if err := os.Chmod(files[0], 0o644); err != nil {
			t.Fatal(err)
		}
		if _, err := LoadKeyset(dir, false, c.now); err == nil || !strings.Contains(err.Error(), "accessible to group or others") {
			t.Fatalf("err = %v", err)
		}
		if _, err := LoadKeyset(dir, true, c.now); err != nil {
			t.Fatalf("the development escape hatch must work: %v", err)
		}
	})
	t.Run("a kid that is a path", func(t *testing.T) {
		dir, c, _ := newKeyset(t)
		write(dir, manifest{Keys: []Entry{{KID: "../evil", PublishAt: 1, UseFrom: 1}}})
		if _, err := LoadKeyset(dir, false, c.now); err == nil {
			t.Fatal("a kid with path characters must be refused before any file is opened")
		}
	})
	t.Run("a duplicate kid", func(t *testing.T) {
		dir, c, ks := newKeyset(t)
		kid := ks.Statuses()[0].KID
		write(dir, manifest{Keys: []Entry{{KID: kid, PublishAt: 1, UseFrom: 1}, {KID: kid, PublishAt: 1, UseFrom: 2}}})
		if _, err := LoadKeyset(dir, false, c.now); err == nil {
			t.Fatal("accepted")
		}
	})
	t.Run("a schedule out of order", func(t *testing.T) {
		dir, c, ks := newKeyset(t)
		kid := ks.Statuses()[0].KID
		write(dir, manifest{Keys: []Entry{{KID: kid, PublishAt: 100, UseFrom: 50}}})
		if _, err := LoadKeyset(dir, false, c.now); err == nil {
			t.Fatal("use_from before publish_at would sign with a key consumers have not seen")
		}
	})
	t.Run("a listed key with no file", func(t *testing.T) {
		dir, c, _ := newKeyset(t)
		write(dir, manifest{Keys: []Entry{{KID: "ghost", PublishAt: 1, UseFrom: 1}}})
		if _, err := LoadKeyset(dir, false, c.now); err == nil {
			t.Fatal("accepted")
		}
	})
	t.Run("a key that is not Ed25519", func(t *testing.T) {
		dir, c, _ := newKeyset(t)
		rk, _ := rsa.GenerateKey(rand.Reader, 2048)
		der, _ := x509.MarshalPKCS8PrivateKey(rk)
		if err := os.WriteFile(filepath.Join(dir, "rsa1.pem"), pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}), 0o600); err != nil {
			t.Fatal(err)
		}
		write(dir, manifest{Keys: []Entry{{KID: "rsa1", PublishAt: 1, UseFrom: 1}}})
		if _, err := LoadKeyset(dir, false, c.now); err == nil || !strings.Contains(err.Error(), "Ed25519") {
			t.Fatalf("err = %v", err)
		}
	})
	t.Run("no key is current", func(t *testing.T) {
		dir, c, ks := newKeyset(t)
		kid := ks.Statuses()[0].KID
		write(dir, manifest{Keys: []Entry{{KID: kid, PublishAt: 1, UseFrom: 2, RetireAt: 3}}}) // retired long before t0
		if _, err := LoadKeyset(dir, false, c.now); !errors.Is(err, ErrNoCurrentKey) {
			t.Fatalf("err = %v, want ErrNoCurrentKey (a replica with nothing to sign with must not become ready)", err)
		}
	})
	t.Run("an empty directory", func(t *testing.T) {
		if _, err := LoadKeyset(t.TempDir(), false, nil); err == nil {
			t.Fatal("accepted")
		}
	})
}

// The rotation lifecycle, with the clock moved by hand (T035): before use_from the old key signs, after it the new one, both are served
// through the overlap, and the old one disappears at retire_at. Nothing here needs a second process or a sleep.
func TestRotationLifecycle(t *testing.T) {
	dir, c, ks := newKeyset(t)
	oldKID := ks.Statuses()[0].KID

	c.t = t0.Add(time.Hour)
	overlap := 10 * time.Minute
	newKID, err := RotateKeyset(dir, c.t, overlap)
	if err != nil {
		t.Fatal(err)
	}
	if err := ks.Reload(); err != nil {
		t.Fatal(err)
	}

	at := func(d time.Duration, wantCurrent string, wantServed ...string) {
		t.Helper()
		c.t = t0.Add(time.Hour).Add(d)
		kid, _, err := ks.Current()
		if err != nil || kid != wantCurrent {
			t.Fatalf("at +%s the signing key is %q (%v), want %q", d, kid, err, wantCurrent)
		}
		var served []string
		for _, p := range ks.Published() {
			served = append(served, p.KID)
		}
		if len(served) != len(wantServed) {
			t.Fatalf("at +%s the served keys are %v, want %v", d, served, wantServed)
		}
		want := map[string]bool{}
		for _, k := range wantServed {
			want[k] = true
		}
		for _, k := range served {
			if !want[k] {
				t.Fatalf("at +%s the served keys are %v, want %v", d, served, wantServed)
			}
		}
	}
	at(0, oldKID, oldKID, newKID)              // the new key is published first, so consumers can cache it before it signs
	at(9*time.Minute, oldKID, oldKID, newKID)  // still the old key signing
	at(10*time.Minute, newKID, oldKID, newKID) // the new key takes over; the old one is still served (retiring)
	at(19*time.Minute, newKID, oldKID, newKID) // an identity the old key signed a moment ago must still verify
	at(20*time.Minute, newKID, newKID)         // the old key's retire_at: no longer served
}

// A running replica must notice a rotation even when the new manifest carries the same modification time as the old one. That is
// not exotic: ext4 stamps files with the kernel's coarse clock (a few milliseconds), some bind mounts keep whole seconds, and `keys init`
// followed by `keys rotate` can land in one tick. CI hit it: the replica kept the old key and served only it, while a restarted peer
// chose the new one. The test makes the collision certain by setting the mtime back by hand.
func TestAReloadSeesARotationThatKeepsTheManifestsModificationTime(t *testing.T) {
	dir, c, ks := newKeyset(t)
	manifestPath := filepath.Join(dir, manifestName)
	before, err := os.Stat(manifestPath)
	if err != nil {
		t.Fatal(err)
	}

	c.t = t0.Add(time.Hour)
	newKID, err := RotateKeyset(dir, c.t, 10*time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(manifestPath, before.ModTime(), before.ModTime()); err != nil {
		t.Fatal(err)
	}
	if err := ks.Reload(); err != nil {
		t.Fatal(err)
	}

	var served []string
	for _, p := range ks.Published() {
		served = append(served, p.KID)
	}
	if !slices.Contains(served, newKID) {
		t.Fatalf("served keys %v do not include the newly published %s: Reload compared timestamps and missed the change", served, newKID)
	}
}

func TestARestartedReplicaChoosesTheSameSigningKeyAsItsPeer(t *testing.T) {
	dir, c, ks := newKeyset(t)
	c.t = t0.Add(time.Hour)
	if _, err := RotateKeyset(dir, c.t, 10*time.Minute); err != nil {
		t.Fatal(err)
	}
	_ = ks.Reload()
	for _, d := range []time.Duration{0, 5 * time.Minute, 10 * time.Minute, 15 * time.Minute, 30 * time.Minute} {
		c.t = t0.Add(time.Hour).Add(d)
		a, _, _ := ks.Current()
		restarted, err := LoadKeyset(dir, false, c.now)
		if err != nil {
			t.Fatal(err)
		}
		b, _, _ := restarted.Current()
		if a != b {
			t.Fatalf("at +%s one replica signs with %s, a restarted one with %s", d, a, b)
		}
	}
}

func TestRotationRefusesToLeapfrogAnotherInProgress(t *testing.T) {
	dir, c, _ := newKeyset(t)
	c.t = t0.Add(time.Hour)
	if _, err := RotateKeyset(dir, c.t, 10*time.Minute); err != nil {
		t.Fatal(err)
	}
	if _, err := RotateKeyset(dir, c.t.Add(time.Minute), 10*time.Minute); err == nil || !strings.Contains(err.Error(), "already in progress") {
		t.Fatalf("err = %v: a second new key would leapfrog the first, which would then never sign anything", err)
	}
	// Once the first has taken over, another rotation is fine.
	if _, err := RotateKeyset(dir, c.t.Add(11*time.Minute), 10*time.Minute); err != nil {
		t.Fatalf("a rotation after the previous one finished must work: %v", err)
	}
	if _, err := RotateKeyset(dir, c.t, 0); err == nil {
		t.Fatal("an overlap of zero would let a key sign before consumers could have seen it")
	}
}

func TestReloadIsANoOpUntilTheManifestChanges(t *testing.T) {
	dir, c, ks := newKeyset(t)
	if err := ks.Reload(); err != nil {
		t.Fatal(err)
	}
	if len(ks.Statuses()) != 1 {
		t.Fatal("unexpected keys")
	}
	c.t = t0.Add(time.Hour)
	if _, err := RotateKeyset(dir, c.t, time.Minute); err != nil {
		t.Fatal(err)
	}
	// Make sure the modification time differs even on a coarse filesystem clock.
	future := time.Now().Add(2 * time.Second)
	_ = os.Chtimes(filepath.Join(dir, manifestName), future, future)
	if err := ks.Reload(); err != nil {
		t.Fatal(err)
	}
	if len(ks.Statuses()) != 2 {
		t.Fatalf("the scheduled rotation was not picked up: %+v", ks.Statuses())
	}
}

func TestAFailedReloadKeepsTheOldKeys(t *testing.T) {
	dir, _, ks := newKeyset(t)
	raw, _ := os.ReadFile(filepath.Join(dir, manifestName))
	if err := os.WriteFile(filepath.Join(dir, manifestName), []byte("{not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	future := time.Now().Add(2 * time.Second)
	_ = os.Chtimes(filepath.Join(dir, manifestName), future, future)
	if err := ks.Reload(); err == nil {
		t.Fatal("a corrupt manifest must be reported")
	}
	if _, _, err := ks.Current(); err != nil {
		t.Fatalf("the old keys must keep signing: %v", err)
	}
	_ = raw
}

func TestListShowsStatesAndNoKeyMaterial(t *testing.T) {
	dir, c, _ := newKeyset(t)
	c.t = t0.Add(time.Hour)
	if _, err := RotateKeyset(dir, c.t, 10*time.Minute); err != nil {
		t.Fatal(err)
	}
	st, err := ListKeyset(dir, c.t)
	if err != nil || len(st) != 2 {
		t.Fatalf("%v %v", st, err)
	}
	states := map[State]int{}
	for _, s := range st {
		states[s.State]++
	}
	if states[Current] != 1 || states[Published] != 1 {
		t.Fatalf("states = %v", states)
	}
	out, _ := json.Marshal(st)
	for _, banned := range []string{"PRIVATE", "BEGIN", "seed", "priv"} {
		if strings.Contains(string(out), banned) {
			t.Errorf("list output contains %q", banned)
		}
	}
}
