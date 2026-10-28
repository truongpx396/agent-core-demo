// Package errenv writes the repository's canonical error envelope, `{"code", "message", "details"}`, the one shape every
// caller-facing error uses (app/core/errors.py, pattern 30), so a client switches on `code` instead of parsing text.
//
// Two invariants live here and nowhere else:
//
//   - The message is fixed text per code (errorcodes.json). It is written for the caller and never contains a token, a claim, an
//     exception's text or anything else that came from a request: a library error can echo its input.
//   - `details` accepts only the keys in allowedDetails. A new key is a conscious change to this file, so a claim value or a token
//     fragment cannot be slipped into a response by a caller deep in the stack.
//
// The registry file is shared with the Python side (PR 2 checks `ErrorCode` against it), which is why the codes are data, not just
// constants: the constants below exist so Go code cannot misspell a code, and a test fails if they and the file disagree.
package errenv

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sort"

	"authlimit"
)

// Code is one of the codes in errorcodes.json.
type Code string

const (
	Unauthenticated Code = "unauthenticated"
	Forbidden       Code = "forbidden"
	RateLimited     Code = "rate_limited"
	AuthUnavailable Code = "auth_unavailable"
	Internal        Code = "internal"
)

// Detail keys a response may carry. Anything else is dropped.
const (
	DetailRequiredScope     = "required_scope"
	DetailRetryAfterSeconds = "retry_after_seconds"
)

var allowedDetails = map[string]bool{
	DetailRequiredScope:     true,
	DetailRetryAfterSeconds: true,
}

type entry struct {
	Status  int    `json:"status"`
	Message string `json:"message"`
}

var registry = mustLoad(authlimit.ErrorCodesJSON)

func mustLoad(data []byte) map[Code]entry {
	var file struct {
		Codes map[Code]entry `json:"codes"`
	}
	if err := json.Unmarshal(data, &file); err != nil {
		panic(fmt.Sprintf("errorcodes.json is not valid JSON: %v", err)) // build-time asset: failing at start beats serving unlabeled errors
	}
	for code, e := range file.Codes {
		if e.Status < 400 || e.Status > 599 || e.Message == "" {
			panic(fmt.Sprintf("errorcodes.json: code %q needs a 4xx/5xx status and a message", code))
		}
	}
	return file.Codes
}

// Codes lists every registered code, sorted (used by the registry-agreement test).
func Codes() []Code {
	out := make([]Code, 0, len(registry))
	for c := range registry {
		out = append(out, c)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// Status is the HTTP status registered for a code.
func Status(c Code) int {
	if e, ok := registry[c]; ok {
		return e.Status
	}
	return http.StatusInternalServerError
}

// Envelope is the response body.
type Envelope struct {
	Code    Code           `json:"code"`
	Message string         `json:"message"`
	Details map[string]any `json:"details"`
}

// New builds an envelope for a code, keeping only the allowed detail keys. An unknown code becomes Internal rather than a body with
// no message.
func New(c Code, details map[string]any) Envelope {
	e, ok := registry[c]
	if !ok {
		c, e = Internal, registry[Internal]
	}
	kept := map[string]any{}
	for k, v := range details {
		if allowedDetails[k] {
			kept[k] = v
		}
	}
	return Envelope{Code: c, Message: e.Message, Details: kept}
}

// Write sends the envelope with the registered status. `extra` headers (WWW-Authenticate, Retry-After) are set first.
func Write(w http.ResponseWriter, c Code, details map[string]any, extra http.Header) {
	for k, vs := range extra {
		for _, v := range vs {
			w.Header().Add(k, v)
		}
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(Status(c))
	_ = json.NewEncoder(w).Encode(New(c, details)) // the status line is already sent; a write error here has nothing left to report to
}
