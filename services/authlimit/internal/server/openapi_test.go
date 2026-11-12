package server

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"authlimit/internal/api"
	"authlimit/internal/errenv"
	"authlimit/internal/extjwt"
	"authlimit/internal/limiter"
	"authlimit/internal/revoke"
)

// openapi.json is written by hand, and the routing and the body types are generated from it (internal/api). CI regenerates the code and
// fails on a difference; these tests are what keep the rest true: the generated routes are the document's (a quick local answer to a
// forgotten `make authlimit-generate`), the error codes it names are the registry's, and each answer a handler gives is one the document
// says it gives, with the headers it says come with it. They read the document the way a client does, from the file.

type document map[string]any

func loadDocument(t *testing.T) document {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "..", "openapi.json"))
	if err != nil {
		t.Fatal(err)
	}
	var d document
	if err := json.Unmarshal(raw, &d); err != nil {
		t.Fatalf("openapi.json is not valid JSON: %v", err)
	}
	if d["openapi"] != "3.1.0" {
		t.Fatalf("openapi = %v", d["openapi"])
	}
	return d
}

func object(t *testing.T, v any) map[string]any {
	t.Helper()
	m, ok := v.(map[string]any)
	if !ok {
		t.Fatalf("expected an object, got %T", v)
	}
	return m
}

// resolve follows a `$ref` into the document (only the local `#/components/...` form this document uses).
func (d document) resolve(t *testing.T, node map[string]any) map[string]any {
	t.Helper()
	ref, ok := node["$ref"].(string)
	if !ok {
		return node
	}
	var cur any = map[string]any(d)
	for _, part := range strings.Split(strings.TrimPrefix(ref, "#/"), "/") {
		cur = object(t, cur)[part]
	}
	return object(t, cur)
}

// operations is "METHOD /path" -> the operation, for every operation the document lists.
func (d document) operations(t *testing.T) map[string]map[string]any {
	t.Helper()
	out := map[string]map[string]any{}
	for path, item := range object(t, d["paths"]) {
		for method, op := range object(t, item) {
			out[strings.ToUpper(method)+" "+path] = object(t, op)
		}
	}
	return out
}

func sorted(set map[string]bool) []string {
	out := make([]string, 0, len(set))
	for k := range set {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// recordingMux notes the patterns registered on it and otherwise behaves as a ServeMux.
type recordingMux struct {
	*http.ServeMux
	patterns []string
}

func (m *recordingMux) HandleFunc(pattern string, h func(http.ResponseWriter, *http.Request)) {
	m.patterns = append(m.patterns, pattern)
	m.ServeMux.HandleFunc(pattern, h)
}

func TestTheGeneratedRoutesAreExactlyTheOnesTheOpenAPIDocumentLists(t *testing.T) {
	documented := map[string]bool{}
	for key := range loadDocument(t).operations(t) {
		documented[key] = true
	}
	rec := &recordingMux{ServeMux: http.NewServeMux()}
	api.HandlerFromMux(newBed(t).srv, rec)
	registered := map[string]bool{}
	for _, pattern := range rec.patterns {
		registered[pattern] = true
	}
	if strings.Join(sorted(documented), "\n") != strings.Join(sorted(registered), "\n") {
		t.Fatalf("openapi.json lists %v, the generated code registers %v: run `make authlimit-generate` and commit the result", sorted(documented), sorted(registered))
	}
}

func TestTheOpenAPIDocumentNamesEveryErrorCodeAndStatusTheServiceCanEmit(t *testing.T) {
	d := loadDocument(t)
	schema := object(t, object(t, object(t, d["components"])["schemas"])["Error"])
	enum := map[string]bool{}
	for _, c := range object(t, object(t, schema["properties"])["code"])["enum"].([]any) {
		enum[c.(string)] = true
	}
	registered := map[string]bool{}
	for _, c := range errenv.Codes() {
		registered[string(c)] = true
	}
	if strings.Join(sorted(enum), ",") != strings.Join(sorted(registered), ",") {
		t.Fatalf("the Error schema's codes %v are not errorcodes.json's %v", sorted(enum), sorted(registered))
	}

	statuses := map[string]bool{}
	for _, op := range d.operations(t) {
		for status := range object(t, op["responses"]) {
			statuses[status] = true
		}
	}
	for _, c := range errenv.Codes() {
		if status := strconv.Itoa(errenv.Status(c)); !statuses[status] {
			t.Errorf("code %q answers %s and no operation in openapi.json documents that status", c, status)
		}
	}
}

func TestEveryAnswerTheHandlersGiveIsDocumentedWithTheHeadersThatComeWithIt(t *testing.T) {
	cases := []struct {
		name    string
		pattern string
		status  int
		call    func(t *testing.T, b *bed) *httptest.ResponseRecorder
	}{
		{"let through", "GET /v1/check", 200, func(t *testing.T, b *bed) *httptest.ResponseRecorder {
			return b.check("POST", "/chat/stream/queued", b.vf.token(t, "valid_chat"))
		}},
		{"no token", "GET /v1/check", 401, func(t *testing.T, b *bed) *httptest.ResponseRecorder {
			return b.check("POST", "/chat/stream/queued", "")
		}},
		{"missing scope", "GET /v1/check", 403, func(t *testing.T, b *bed) *httptest.ResponseRecorder {
			return b.check("POST", "/chat/stream/queued", b.vf.token(t, "missing_scope"))
		}},
		{"bucket empty", "GET /v1/check", 429, func(t *testing.T, b *bed) *httptest.ResponseRecorder {
			b.lim.decision = limiter.Decision{Allowed: false, RetryAfter: 6 * time.Second}
			return b.check("POST", "/chat/stream/queued", b.vf.token(t, "valid_chat"))
		}},
		{"revocation copy untrusted", "GET /v1/check", 503, func(t *testing.T, b *bed) *httptest.ResponseRecorder {
			b.rev.verdict = revoke.Unavailable
			return b.check("POST", "/chat/stream/queued", b.vf.token(t, "valid_chat"))
		}},
		{"address let through", "GET /v1/limit/signin", 200, func(t *testing.T, b *bed) *httptest.ResponseRecorder {
			return b.limit("/v1/limit/signin", "203.0.113.7")
		}},
		{"address bucket empty", "GET /v1/limit/webhook", 429, func(t *testing.T, b *bed) *httptest.ResponseRecorder {
			b.lim.decision = limiter.Decision{Allowed: false, RetryAfter: 3 * time.Second}
			return b.limit("/v1/limit/webhook", "203.0.113.7")
		}},
		{"alive", "GET /healthz", 200, func(t *testing.T, b *bed) *httptest.ResponseRecorder { return get(b.h, "/healthz") }},
		{"ready", "GET /readyz", 200, func(t *testing.T, b *bed) *httptest.ResponseRecorder { return get(b.h, "/readyz") }},
		{"not ready", "GET /readyz", 503, func(t *testing.T, b *bed) *httptest.ResponseRecorder {
			b.keys.state = extjwt.KeysUnusable
			return get(b.h, "/readyz")
		}},
		{"public keys", "GET /internal/jwks.json", 200, func(t *testing.T, b *bed) *httptest.ResponseRecorder { return get(b.h, "/internal/jwks.json") }},
	}
	// The headers this service sets on an answer that a client or a proxy acts on; Content-Type is the body's, checked below.
	described := []string{"WWW-Authenticate", "Retry-After", "X-Internal-Identity", "Cache-Control"}

	d := loadDocument(t)
	ops := d.operations(t)
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			rec := c.call(t, newBed(t))
			if rec.Code != c.status {
				t.Fatalf("the case expects %d and the handler answered %d: %s", c.status, rec.Code, rec.Body.String())
			}
			op, ok := ops[c.pattern]
			if !ok {
				t.Fatalf("no operation %q in openapi.json", c.pattern)
			}
			response := d.resolve(t, object(t, object(t, op["responses"])[strconv.Itoa(rec.Code)]))

			declared := map[string]bool{}
			if headers, ok := response["headers"].(map[string]any); ok {
				for name := range headers {
					declared[http.CanonicalHeaderKey(name)] = true
				}
			}
			for _, name := range described {
				name = http.CanonicalHeaderKey(name) // `declared` is keyed the way net/http keys a header: "WWW-Authenticate" is "Www-Authenticate"
				sent := rec.Header().Get(name) != ""
				if sent && !declared[name] {
					t.Errorf("the handler sends %s on a %d and openapi.json does not say so", name, rec.Code)
				}
				if declared[name] && !sent {
					t.Errorf("openapi.json says %s comes with a %d and the handler did not send it", name, rec.Code)
				}
			}

			if _, hasBody := response["content"]; hasBody {
				if got := rec.Header().Get("Content-Type"); !strings.HasPrefix(got, "application/json") {
					t.Errorf("openapi.json declares a JSON body and the handler sent Content-Type %q", got)
				}
			} else if rec.Body.Len() != 0 {
				t.Errorf("openapi.json declares no body and the handler sent %q", rec.Body.String())
			}
		})
	}
}

func TestTheAddressLimitsInTheTableAndTheOperationsInTheDocumentAreTheSameList(t *testing.T) {
	// Caddy asks for an address limit by a path, and the path exists only because the document declares it (the routes are generated).
	// A limit in the table with no operation can never be asked for, and an operation with no limit answers a 500, so either direction
	// of drift is a defect the build must show.
	raw, err := os.ReadFile(filepath.Join("..", "..", "routes.json"))
	if err != nil {
		t.Fatal(err)
	}
	var table struct {
		AddressLimits map[string]json.RawMessage `json:"address_limits"`
	}
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatal(err)
	}
	inTable := map[string]bool{}
	for name := range table.AddressLimits {
		inTable["GET /v1/limit/"+name] = true
	}
	inDocument := map[string]bool{}
	for key := range loadDocument(t).operations(t) {
		if strings.HasPrefix(key, "GET /v1/limit/") {
			inDocument[key] = true
		}
	}
	if strings.Join(sorted(inTable), "\n") != strings.Join(sorted(inDocument), "\n") {
		t.Fatalf("routes.json address_limits give %v and openapi.json declares %v", sorted(inTable), sorted(inDocument))
	}
}
