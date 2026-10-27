package identity

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// This file is what `authlimit keys init|rotate` does to the key directory. It never prints or returns key material.

func newKID(now time.Time) (string, error) {
	var b [3]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return "k" + now.UTC().Format("20060102t150405") + "-" + hex.EncodeToString(b[:]), nil
}

// writeKey creates <kid>.pem with mode 0600; it refuses to overwrite.
func writeKey(dir, kid string, key ed25519.PrivateKey) error {
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return err
	}
	f, err := os.OpenFile(filepath.Join(dir, kid+".pem"), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return err
	}
	if err := pem.Encode(f, &pem.Block{Type: "PRIVATE KEY", Bytes: der}); err != nil {
		_ = f.Close()
		return err
	}
	return f.Close()
}

// writeManifest replaces keyset.json atomically (write a temp file, rename), so a replica reading it never sees half a file.
func writeManifest(dir string, m manifest) error {
	raw, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".keyset-*.tmp")
	if err != nil {
		return err
	}
	name := tmp.Name()
	if _, err := tmp.Write(append(raw, '\n')); err != nil {
		_ = tmp.Close()
		_ = os.Remove(name)
		return err
	}
	if err := tmp.Chmod(0o600); err != nil {
		_ = tmp.Close()
		_ = os.Remove(name)
		return err
	}
	if err := tmp.Close(); err != nil {
		_ = os.Remove(name)
		return err
	}
	return os.Rename(name, filepath.Join(dir, manifestName))
}

func readManifest(dir string) (manifest, error) {
	var m manifest
	raw, err := os.ReadFile(filepath.Join(dir, manifestName))
	if err != nil {
		return m, err
	}
	return m, json.Unmarshal(raw, &m)
}

// InitKeyset creates the directory (mode 0700) and the first key, current immediately. It refuses to touch a directory that already has a
// keyset: replacing a signing key silently would invalidate every identity in flight and hide that it happened.
func InitKeyset(dir string, now time.Time) (string, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	if err := os.Chmod(dir, 0o700); err != nil {
		return "", err
	}
	if _, err := os.Stat(filepath.Join(dir, manifestName)); err == nil {
		return "", errors.New("keyset already exists; use `keys rotate` to add a key")
	}
	_, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return "", err
	}
	kid, err := newKID(now)
	if err != nil {
		return "", err
	}
	if err := writeKey(dir, kid, priv); err != nil {
		return "", err
	}
	t := now.Unix()
	return kid, writeManifest(dir, manifest{Keys: []Entry{{KID: kid, PublishAt: t, UseFrom: t}}})
}

// RotateKeyset adds a key that is published now and becomes current after `overlap`, and schedules the key that is current today to
// retire `overlap` after that. The overlap is what lets a consumer that cached the old public set (for up to its cache time) learn the
// new key BEFORE it signs, and keeps the old key served until no identity it signed can still be alive.
//
// It refuses while another rotation is in progress (a key published but not yet current), because a second new key would leapfrog the
// first and the first would never sign anything.
func RotateKeyset(dir string, now time.Time, overlap time.Duration) (string, error) {
	if overlap < time.Second {
		return "", errors.New("overlap must be at least one second")
	}
	m, err := readManifest(dir)
	if err != nil {
		return "", fmt.Errorf("cannot read the keyset (run `keys init` first): %w", err)
	}
	t := now.Unix()
	cur := currentKID(m.Keys, t)
	if cur == "" {
		return "", ErrNoCurrentKey
	}
	for _, e := range m.Keys {
		if e.UseFrom > t && (e.RetireAt == 0 || e.RetireAt > t) {
			return "", fmt.Errorf("a rotation is already in progress: key %s is published and becomes current at %s", e.KID, time.Unix(e.UseFrom, 0).UTC().Format(time.RFC3339))
		}
	}
	_, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return "", err
	}
	kid, err := newKID(now)
	if err != nil {
		return "", err
	}
	if err := writeKey(dir, kid, priv); err != nil {
		return "", err
	}
	useFrom := now.Add(overlap).Unix()
	retire := now.Add(2 * overlap).Unix()
	for i := range m.Keys {
		if m.Keys[i].KID == cur {
			m.Keys[i].RetireAt = retire
		}
	}
	m.Keys = append(m.Keys, Entry{KID: kid, PublishAt: t, UseFrom: useFrom})
	return kid, writeManifest(dir, m)
}

// ListKeyset reads the directory's schedule at `now` without loading any private key.
func ListKeyset(dir string, now time.Time) ([]Status, error) {
	m, err := readManifest(dir)
	if err != nil {
		return nil, err
	}
	t := now.Unix()
	cur := currentKID(m.Keys, t)
	out := make([]Status, 0, len(m.Keys))
	for _, e := range m.Keys {
		out = append(out, Status{Entry: e, State: stateOf(e, cur, t)})
	}
	return out, nil
}
