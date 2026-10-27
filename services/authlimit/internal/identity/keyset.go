// Package identity owns the internal identity: the short-lived, asymmetrically signed assertion the service hands the front door after it
// has verified a caller's access token, and the keys that sign it.
//
// The API verifies the assertion with PUBLIC keys only. Whoever holds the private key can mint an identity for any tenant, so the private
// key lives in one place (this process's mounted key directory), never in an image, a log or Redis, and no consumer ever holds it (threat
// T6). An HMAC secret shared with the consumers would have made every consumer able to forge one.
//
// keyset.go is the key directory:
//
//	keyset.json     {"keys": [{"kid", "publish_at", "use_from", "retire_at"}, ...]}   (Unix seconds)
//	<kid>.pem       a PKCS#8 Ed25519 private key
//
// A key's STATE is not stored; it is derived from the clock, so every replica reading the same directory agrees without talking to
// another replica, and a replica restarted mid-rotation chooses the same signing key as its peer:
//
//	now < publish_at           pending    not served, not used
//	publish_at <= now          published  its public key is served so consumers can cache it BEFORE it signs anything
//	use_from <= now            current    the newest such key signs
//	a newer key is current     retiring   still served, so an identity it signed a moment ago still verifies
//	retire_at <= now           retired    no longer served
//
// Rotation therefore needs no coordination and no downtime: publish the new key, wait out the consumers' cache time, let it become
// current, keep the old one served until nothing it signed can still be alive.
package identity

import (
	"bytes"
	"crypto/ed25519"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"sync"
	"time"
)

const manifestName = "keyset.json"

var kidPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)

// Entry is one key's schedule.
type Entry struct {
	KID       string `json:"kid"`
	PublishAt int64  `json:"publish_at"`
	UseFrom   int64  `json:"use_from"`
	RetireAt  int64  `json:"retire_at"` // 0: never retired
}

type manifest struct {
	Keys []Entry `json:"keys"`
}

// State is a key's place in its life.
type State string

const (
	Pending   State = "pending"
	Published State = "published"
	Current   State = "current"
	Retiring  State = "retiring"
	Retired   State = "retired"
)

// ErrNoCurrentKey means no key is currently allowed to sign: the replica must not become ready.
var ErrNoCurrentKey = errors.New("no current signing key")

// Keyset is the loaded key directory. It is safe for concurrent use.
type Keyset struct {
	dir        string
	loosePerms bool
	now        func() time.Time

	mu      sync.RWMutex
	entries []Entry
	priv    map[string]ed25519.PrivateKey
	loaded  []byte // the manifest bytes the keys above came from; Reload compares content, not a timestamp
}

// LoadKeyset reads the directory. It fails if the manifest or any key it lists cannot be read, a kid is not a plain word, a key file is
// accessible to group or others (unless loosePerms, for a development bind mount whose ownership Docker maps), or no key is current.
func LoadKeyset(dir string, loosePerms bool, now func() time.Time) (*Keyset, error) {
	if now == nil {
		now = time.Now
	}
	k := &Keyset{dir: dir, loosePerms: loosePerms, now: now}
	if err := k.load(); err != nil {
		return nil, err
	}
	if _, _, err := k.Current(); err != nil {
		return nil, err
	}
	return k, nil
}

// Reload re-reads the directory if the manifest changed (a rotation was scheduled). A failed reload keeps the old keys; the error is
// returned so the caller can log it.
//
// "Changed" is the manifest's bytes, not its modification time. A timestamp has the filesystem's granularity (a few milliseconds on ext4,
// a whole second on some bind mounts), so a rotation written right after the previous manifest could carry the same mtime and be
// ignored for good: that replica would keep signing with the old key after its peers had moved on. The file is a few hundred bytes.
func (k *Keyset) Reload() error {
	raw, err := os.ReadFile(filepath.Join(k.dir, manifestName))
	if err != nil {
		return fmt.Errorf("keyset: %w", err)
	}
	k.mu.RLock()
	unchanged := bytes.Equal(raw, k.loaded)
	k.mu.RUnlock()
	if unchanged {
		return nil
	}
	return k.load()
}

func (k *Keyset) load() error {
	path := filepath.Join(k.dir, manifestName)
	info, err := os.Stat(path)
	if err != nil {
		return fmt.Errorf("keyset: cannot read %s: %w", manifestName, err)
	}
	if err := k.checkMode(path, info); err != nil {
		return err
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("keyset: cannot read %s: %w", manifestName, err)
	}
	var m manifest
	if err := json.Unmarshal(raw, &m); err != nil {
		return fmt.Errorf("keyset: %s is not valid JSON: %w", manifestName, err)
	}
	if len(m.Keys) == 0 {
		return errors.New("keyset: no keys listed (run `authlimit keys init`)")
	}
	priv := map[string]ed25519.PrivateKey{}
	seen := map[string]bool{}
	for _, e := range m.Keys {
		if !kidPattern.MatchString(e.KID) {
			return fmt.Errorf("keyset: key id %q must be 1-64 letters, digits, - or _", e.KID)
		}
		if seen[e.KID] {
			return fmt.Errorf("keyset: key id %q is listed twice", e.KID)
		}
		seen[e.KID] = true
		if e.UseFrom < e.PublishAt || (e.RetireAt != 0 && e.RetireAt <= e.UseFrom) {
			return fmt.Errorf("keyset: key %q needs publish_at <= use_from < retire_at", e.KID)
		}
		key, err := k.readKey(e.KID)
		if err != nil {
			return err
		}
		priv[e.KID] = key
	}
	k.mu.Lock()
	k.entries, k.priv, k.loaded = m.Keys, priv, raw
	k.mu.Unlock()
	return nil
}

func (k *Keyset) checkMode(path string, info os.FileInfo) error {
	if !k.loosePerms && info.Mode().Perm()&0o077 != 0 {
		return fmt.Errorf("keyset: %s is accessible to group or others (mode %04o); chmod 600 it, or set AUTHLIMIT_KEYS_ALLOW_LOOSE_PERMS=true for a development bind mount", filepath.Base(path), info.Mode().Perm())
	}
	return nil
}

func (k *Keyset) readKey(kid string) (ed25519.PrivateKey, error) {
	path := filepath.Join(k.dir, kid+".pem")
	info, err := os.Stat(path)
	if err != nil {
		return nil, fmt.Errorf("keyset: key %q is listed but its file cannot be read: %w", kid, err)
	}
	if err := k.checkMode(path, info); err != nil {
		return nil, err
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("keyset: key %q: %w", kid, err)
	}
	block, _ := pem.Decode(raw)
	if block == nil || block.Type != "PRIVATE KEY" {
		return nil, fmt.Errorf("keyset: key %q is not a PKCS#8 PEM private key", kid)
	}
	parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("keyset: key %q cannot be parsed: %w", kid, err) // the error text is the parser's, never key bytes
	}
	key, ok := parsed.(ed25519.PrivateKey)
	if !ok {
		return nil, fmt.Errorf("keyset: key %q is not an Ed25519 key", kid)
	}
	return key, nil
}

// StateOf is the state of an entry at time t, given the entries around it.
func stateOf(e Entry, current string, t int64) State {
	switch {
	case t < e.PublishAt:
		return Pending
	case e.RetireAt != 0 && t >= e.RetireAt:
		return Retired
	case e.KID == current:
		return Current
	case t < e.UseFrom:
		return Published
	default:
		return Retiring // it was usable and a newer key has taken over
	}
}

// currentKID is the newest key (by use_from) that is usable now: use_from has passed and it is not retired.
func currentKID(entries []Entry, t int64) string {
	best, bestFrom := "", int64(-1)
	for _, e := range entries {
		if t >= e.UseFrom && (e.RetireAt == 0 || t < e.RetireAt) && e.UseFrom > bestFrom {
			best, bestFrom = e.KID, e.UseFrom
		}
	}
	return best
}

// Current returns the key that signs now.
func (k *Keyset) Current() (kid string, key ed25519.PrivateKey, err error) {
	k.mu.RLock()
	defer k.mu.RUnlock()
	kid = currentKID(k.entries, k.now().Unix())
	if kid == "" {
		return "", nil, ErrNoCurrentKey
	}
	return kid, k.priv[kid], nil
}

// CurrentSince is when the current key became current (for the signing-key-age gauge).
func (k *Keyset) CurrentSince() (time.Time, bool) {
	k.mu.RLock()
	defer k.mu.RUnlock()
	kid := currentKID(k.entries, k.now().Unix())
	for _, e := range k.entries {
		if e.KID == kid {
			return time.Unix(e.UseFrom, 0), true
		}
	}
	return time.Time{}, false
}

// Status is one key's kid, state and schedule, without key material.
type Status struct {
	Entry
	State State `json:"state"`
}

// Statuses lists every key with its state now, oldest first.
func (k *Keyset) Statuses() []Status {
	k.mu.RLock()
	defer k.mu.RUnlock()
	t := k.now().Unix()
	cur := currentKID(k.entries, t)
	out := make([]Status, 0, len(k.entries))
	for _, e := range k.entries {
		out = append(out, Status{Entry: e, State: stateOf(e, cur, t)})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].UseFrom < out[j].UseFrom })
	return out
}

// PublicKey is a key id and its Ed25519 public key.
type PublicKey struct {
	KID string
	Key ed25519.PublicKey
}

// Published are the public keys consumers should accept now: published, current and retiring keys, oldest first.
func (k *Keyset) Published() []PublicKey {
	k.mu.RLock()
	defer k.mu.RUnlock()
	t := k.now().Unix()
	cur := currentKID(k.entries, t)
	var out []PublicKey
	for _, e := range k.entries {
		switch stateOf(e, cur, t) {
		case Published, Current, Retiring:
			out = append(out, PublicKey{KID: e.KID, Key: k.priv[e.KID].Public().(ed25519.PublicKey)})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].KID < out[j].KID })
	return out
}
