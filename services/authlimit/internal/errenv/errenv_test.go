package errenv

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestConstantsAndRegistryAgree(t *testing.T) {
	// The Go constants and errorcodes.json must name the same codes: the file is what the Python side checks `ErrorCode` against.
	want := map[Code]bool{Unauthenticated: true, Forbidden: true, RateLimited: true, AuthUnavailable: true, Internal: true}
	got := Codes()
	if len(got) != len(want) {
		t.Fatalf("registry has %d codes %v, constants name %d", len(got), got, len(want))
	}
	for _, c := range got {
		if !want[c] {
			t.Errorf("code %q is in errorcodes.json but has no Go constant", c)
		}
	}
}

func TestStatuses(t *testing.T) {
	for code, want := range map[Code]int{Unauthenticated: 401, Forbidden: 403, RateLimited: 429, AuthUnavailable: 503, Internal: 500} {
		if got := Status(code); got != want {
			t.Errorf("Status(%s) = %d, want %d", code, got, want)
		}
	}
}

func TestEnvelopeShapeAndHeaders(t *testing.T) {
	rec := httptest.NewRecorder()
	Write(rec, RateLimited, map[string]any{DetailRetryAfterSeconds: 7}, http.Header{"Retry-After": {"7"}})
	if rec.Code != 429 {
		t.Fatalf("status = %d", rec.Code)
	}
	if rec.Header().Get("Retry-After") != "7" || rec.Header().Get("Content-Type") != "application/json" {
		t.Fatalf("headers = %v", rec.Header())
	}
	var body struct {
		Code    string         `json:"code"`
		Message string         `json:"message"`
		Details map[string]any `json:"details"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body.Code != "rate_limited" || body.Message == "" || body.Details["retry_after_seconds"] != float64(7) {
		t.Fatalf("body = %+v", body)
	}
}

func TestDetailsAreAllowListed(t *testing.T) {
	// A claim value or a token fragment must not be able to ride into a response through `details`.
	e := New(Forbidden, map[string]any{DetailRequiredScope: "api:chat", "tenant": "ecorp", "token": "eyJhbGciOi"})
	if len(e.Details) != 1 || e.Details[DetailRequiredScope] != "api:chat" {
		t.Fatalf("details = %v", e.Details)
	}
}

func TestDetailsAlwaysAnObject(t *testing.T) {
	// `details` is `{}`, never null: a client can index into it without a nil check.
	b, _ := json.Marshal(New(Unauthenticated, nil))
	var raw map[string]json.RawMessage
	_ = json.Unmarshal(b, &raw)
	if string(raw["details"]) != "{}" {
		t.Fatalf("details serialised as %s", raw["details"])
	}
}

func TestUnknownCodeBecomesInternal(t *testing.T) {
	e := New(Code("made_up"), nil)
	if e.Code != Internal || e.Message == "" {
		t.Fatalf("envelope = %+v", e)
	}
}
