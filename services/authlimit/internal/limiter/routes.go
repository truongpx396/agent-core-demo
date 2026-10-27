// Package limiter holds the route-class table and the token-bucket limiter.
//
// routes.go is the table: which routes are public (not checked), which are verified but never limited (cancel, reads), which are
// verified and limited (a turn, an upload), and which are the MCP routes (limit-only). It is data (routes.json), not code scattered
// across handlers, so one reviewer reads the whole policy in one file, and PR 2 can test that every route the API serves appears in it.
//
// The table decides which requests skip authentication ("public"), so classification is deliberately strict. A path that is not exactly
// what it looks like is never public: dot segments, encoded slashes, backslashes, NULs, empty segments and undecodable escapes all fall
// back to the fallback class (verified and limited like a turn). Caddy's own matcher makes a first decision on the same paths; a mistake
// in either one is caught by the other, because public in one place and verified in the other resolves to "verified".
package limiter

import (
	"encoding/json"
	"fmt"
	"net/url"
	"strings"
)

// Class is what the service does with a route.
type Class string

const (
	Turn   Class = "turn"   // verified, limited
	Upload Class = "upload" // verified, limited (its own bucket parameters)
	Read   Class = "read"   // verified, never limited
	Cancel Class = "cancel" // verified, never limited: stopping a runaway turn must never be throttled
	Public Class = "public" // not checked
	MCP    Class = "mcp"    // limit-only: the MCP gateway verifies the token itself
)

// Bucket is one token bucket's shape: how many tokens it holds when full and how fast it refills.
type Bucket struct {
	Capacity  int `json:"capacity"`
	PerMinute int `json:"per_minute"`
}

// Limits are the two buckets a limited class charges: the tenant's and the person's.
type Limits struct {
	Tenant Bucket `json:"tenant"`
	Person Bucket `json:"person"`
}

type segment struct {
	literal string
	param   bool // {name}: any one non-empty segment
}

type rule struct {
	method string // upper case, or "*"
	segs   []segment
	tail   bool // a trailing "*": zero or more further segments
	class  Class
}

// Table is the parsed route-class table.
type Table struct {
	rules    []rule
	limits   map[Class]Limits
	fallback Class
}

type tableFile struct {
	Fallback Class            `json:"fallback"`
	Limits   map[Class]Limits `json:"limits"`
	Routes   []struct {
		Method  string `json:"method"`
		Pattern string `json:"pattern"`
		Class   Class  `json:"class"`
	} `json:"routes"`
}

var knownClasses = map[Class]bool{Turn: true, Upload: true, Read: true, Cancel: true, Public: true, MCP: true}
var limitedClasses = map[Class]bool{Turn: true, Upload: true, MCP: true}

// ParseTable parses and validates routes.json. A table that names an unknown class, a limited class without limits, or a fallback that
// is not limited is refused: a typo must stop the start, not quietly leave a route unmetered.
func ParseTable(data []byte) (*Table, error) {
	var f tableFile
	dec := json.NewDecoder(strings.NewReader(string(data)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&f); err != nil {
		return nil, fmt.Errorf("routes table: %w", err)
	}
	if !limitedClasses[f.Fallback] {
		return nil, fmt.Errorf("routes table: fallback %q must be a limited class (turn, upload or mcp): an unlisted route is verified and limited", f.Fallback)
	}
	for c, l := range f.Limits {
		if !limitedClasses[c] {
			return nil, fmt.Errorf("routes table: limits for %q, which is not a limited class", c)
		}
		for who, b := range map[string]Bucket{"tenant": l.Tenant, "person": l.Person} {
			if b.Capacity < 1 || b.Capacity > 10000 || b.PerMinute < 1 || b.PerMinute > 1000000 {
				return nil, fmt.Errorf("routes table: %s bucket of %q needs capacity 1..10000 and per_minute 1..1000000", who, c)
			}
		}
	}
	for c := range limitedClasses {
		if _, ok := f.Limits[c]; !ok {
			return nil, fmt.Errorf("routes table: no limits for the limited class %q", c)
		}
	}
	t := &Table{limits: f.Limits, fallback: f.Fallback}
	for i, r := range f.Routes {
		if !knownClasses[r.Class] {
			return nil, fmt.Errorf("routes table: route %d names the unknown class %q", i, r.Class)
		}
		method := strings.ToUpper(strings.TrimSpace(r.Method))
		if method == "" {
			return nil, fmt.Errorf("routes table: route %d has no method (use * for any)", i)
		}
		segs, tail, err := parsePattern(r.Pattern)
		if err != nil {
			return nil, fmt.Errorf("routes table: route %d: %w", i, err)
		}
		t.rules = append(t.rules, rule{method: method, segs: segs, tail: tail, class: r.Class})
	}
	return t, nil
}

func parsePattern(p string) ([]segment, bool, error) {
	if !strings.HasPrefix(p, "/") {
		return nil, false, fmt.Errorf("pattern %q must start with /", p)
	}
	parts := splitPath(p)
	var segs []segment
	tail := false
	for i, s := range parts {
		switch {
		case s == "*":
			if i != len(parts)-1 {
				return nil, false, fmt.Errorf("pattern %q: * is only allowed as the last segment", p)
			}
			tail = true
		case strings.HasPrefix(s, "{") && strings.HasSuffix(s, "}") && len(s) > 2:
			segs = append(segs, segment{param: true})
		case strings.ContainsAny(s, "{}*"):
			return nil, false, fmt.Errorf("pattern %q: a segment is either a literal, {name} or a trailing *", p)
		default:
			segs = append(segs, segment{literal: s})
		}
	}
	return segs, tail, nil
}

// splitPath splits "/a/b/" into ["a", "b"] (a trailing slash is not significant) and "/" into [].
func splitPath(p string) []string {
	p = strings.Trim(p, "/")
	if p == "" {
		return nil
	}
	return strings.Split(p, "/")
}

// Classify returns the class for a request. `listed` is false when no rule matched (or the path was not clean) and the fallback was used.
// `rawPath` is the path as the client sent it, query string included or not.
func (t *Table) Classify(method, rawPath string) (class Class, listed bool) {
	segs, ok := cleanSegments(rawPath)
	if !ok {
		return t.fallback, false
	}
	method = strings.ToUpper(method)
	for _, r := range t.rules {
		if r.method != "*" && r.method != method {
			continue
		}
		if matches(r, segs) {
			return r.class, true
		}
	}
	return t.fallback, false
}

func matches(r rule, segs []string) bool {
	if len(segs) < len(r.segs) || (!r.tail && len(segs) != len(r.segs)) {
		return false
	}
	for i, want := range r.segs {
		if want.param {
			if segs[i] == "" {
				return false
			}
			continue
		}
		if segs[i] != want.literal {
			return false
		}
	}
	return true
}

// cleanSegments drops the query, decodes the path once, and refuses anything that is not a plain path: it returns ok=false for an
// undecodable escape, a NUL, a backslash, a dot segment, an empty segment ("//") or an encoded slash. Such a path is never matched
// against a rule, so it can never be classified public.
func cleanSegments(raw string) ([]string, bool) {
	if i := strings.IndexAny(raw, "?#"); i >= 0 {
		raw = raw[:i]
	}
	if !strings.HasPrefix(raw, "/") {
		return nil, false
	}
	lower := strings.ToLower(raw)
	if strings.Contains(lower, "%2f") || strings.Contains(lower, "%5c") || strings.Contains(raw, "\\") || strings.Contains(raw, "\x00") {
		return nil, false
	}
	decoded, err := url.PathUnescape(raw)
	if err != nil || strings.ContainsAny(decoded, "\x00\\") {
		return nil, false
	}
	trimmed := strings.TrimSuffix(decoded, "/")
	if trimmed == "" {
		return nil, true // the root
	}
	segs := strings.Split(strings.TrimPrefix(trimmed, "/"), "/")
	for _, s := range segs {
		if s == "" || s == "." || s == ".." {
			return nil, false
		}
	}
	return segs, true
}

// CleanPath returns the canonical form of a request path ("/a/b", no query, no trailing slash, decoded once) and whether the path was
// plain. A path that is not plain has no canonical form and must never be echoed into a header or matched against a rule.
func CleanPath(raw string) (string, bool) {
	segs, ok := cleanSegments(raw)
	if !ok {
		return "", false
	}
	return "/" + strings.Join(segs, "/"), true
}

// Limits returns the buckets a limited class charges. ok is false for classes that are never limited.
func (t *Table) Limits(c Class) (Limits, bool) {
	l, ok := t.limits[c]
	return l, ok
}

// HasClass reports whether any route is in the class (used to decide whether the MCP metadata template is required).
func (t *Table) HasClass(c Class) bool {
	for _, r := range t.rules {
		if r.class == c {
			return true
		}
	}
	return false
}
