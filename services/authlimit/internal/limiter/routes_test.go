package limiter

import (
	"strings"
	"testing"

	"authlimit"
)

func defaultTable(t *testing.T) *Table {
	t.Helper()
	tbl, err := ParseTable(authlimit.RoutesJSON)
	if err != nil {
		t.Fatalf("the embedded table does not parse: %v", err)
	}
	return tbl
}

func TestEveryRouteTheAPIServesIsInTheDefaultTable(t *testing.T) {
	// The routes the chat API serves today (app/api/routers/*), with the class each must have. PR 2 adds a Python test that walks the
	// real FastAPI routes against this same file; this one pins the intent.
	tbl := defaultTable(t)
	tests := []struct {
		method, path string
		want         Class
	}{
		{"GET", "/", Public},
		{"GET", "/health", Public},
		{"GET", "/health/ready", Public},
		{"GET", "/static/auth.js", Public},
		{"POST", "/billing/webhooks/polar", Public},
		{"GET", "/.well-known/oauth-protected-resource/ecorp/mcp", Public},
		{"OPTIONS", "/chat/stream/queued", Public}, // a CORS preflight carries no credentials
		{"POST", "/chat/stream/queued", Turn},
		{"POST", "/chat/resume", Turn},
		{"POST", "/ingest/upload", Upload},
		{"POST", "/chat/cancel", Cancel},
		{"GET", "/chat/sessions", Read},
		{"GET", "/chat/sessions/abc-123/messages", Read},
		{"GET", "/chat/sessions/abc-123/pending_approval", Read},
		{"GET", "/usage", Read},
		{"GET", "/ingest/stream/job-9", Read},
		{"GET", "/openapi.json", Read}, // FastAPI's own pages: verified, and never charged to a person's turn bucket
		{"GET", "/docs", Read},
		{"GET", "/docs/oauth2-redirect", Read},
		{"GET", "/redoc", Read},
		{"POST", "/ecorp/mcp", MCP},
		{"GET", "/ecorp/mcp", MCP},
		{"DELETE", "/ops/mcp", MCP},
	}
	for _, tc := range tests {
		got, listed := tbl.Classify(tc.method, tc.path)
		if got != tc.want || !listed {
			t.Errorf("%s %s = %s (listed %v), want %s", tc.method, tc.path, got, listed, tc.want)
		}
	}
}

func TestUnlistedRoutesAreVerifiedAndLimitedLikeATurn(t *testing.T) {
	tbl := defaultTable(t)
	for _, tc := range [][2]string{
		{"GET", "/admin"},
		{"POST", "/chat/sessions"},             // listed for GET only
		{"DELETE", "/chat/cancel"},             // listed for POST only
		{"GET", "/chat/sessions/a/b/messages"}, // a {param} is one segment
		{"GET", "/chat/sessions//messages"},    // and not an empty one
		{"GET", "/ecorp/mcp/extra"},            // exact patterns are exact
	} {
		got, listed := tbl.Classify(tc[0], tc[1])
		if got != Turn || listed {
			t.Errorf("%s %s = %s (listed %v), want the fallback", tc[0], tc[1], got, listed)
		}
	}
}

func TestQueryStringAndTrailingSlashAreNotSignificant(t *testing.T) {
	tbl := defaultTable(t)
	for _, p := range []string{"/chat/sessions?limit=5", "/chat/sessions/", "/chat/sessions/?x=1", "/chat/sessions#frag"} {
		if got, listed := tbl.Classify("GET", p); got != Read || !listed {
			t.Errorf("GET %s = %s (listed %v), want read", p, got, listed)
		}
	}
	if got, _ := tbl.Classify("GET", "/?a=b"); got != Public {
		t.Errorf("the root with a query = %s, want public", got)
	}
}

func TestAPathThatIsNotPlainIsNeverPublic(t *testing.T) {
	// The table decides which requests skip authentication, so a trick must not reach "public". Each of these starts like a public
	// prefix and must fall back to the verified-and-limited class.
	tbl := defaultTable(t)
	for _, p := range []string{
		"/static/../chat/stream/queued",
		"/static/%2e%2e/chat/stream/queued",
		"/static/%2E%2E/chat/stream/queued",
		"/static/./x",
		"/static//x",
		"/static/a%2fb",
		"/static/a%2Fb",
		"/static/a%5cb",
		"/static/a\\b",
		"/static/%00",
		"/static/%zz",
		"/billing/webhooks/../../chat/resume",
		"/health/../chat/resume",
		"chat/resume",
		"",
	} {
		got, listed := tbl.Classify("GET", p)
		if got == Public || listed {
			t.Errorf("GET %q = %s (listed %v): must fall back, never public", p, got, listed)
		}
	}
}

func TestPercentEncodedLettersAreDecodedOnce(t *testing.T) {
	// The API decodes %63 to c before routing, so /chat/%63ancel IS /chat/cancel; the table must agree, in both directions.
	tbl := defaultTable(t)
	if got, _ := tbl.Classify("POST", "/chat/%63ancel"); got != Cancel {
		t.Errorf("/chat/%%63ancel = %s, want cancel", got)
	}
	if got, listed := tbl.Classify("POST", "/chat/%2563ancel"); listed || got != Turn {
		t.Errorf("a double-encoded path = %s (listed %v), want the fallback", got, listed)
	}
}

func TestLimits(t *testing.T) {
	tbl := defaultTable(t)
	l, ok := tbl.Limits(Turn)
	if !ok || l.Tenant != (Bucket{Capacity: 10, PerMinute: 30}) || l.Person != (Bucket{Capacity: 5, PerMinute: 10}) {
		t.Fatalf("turn limits = %+v ok=%v", l, ok)
	}
	for _, c := range []Class{Read, Cancel, Public} {
		if _, limited := tbl.Limits(c); limited {
			t.Errorf("%s must never be limited", c)
		}
	}
	if !tbl.HasClass(MCP) || !tbl.HasClass(Cancel) {
		t.Error("HasClass is wrong for classes in the default table")
	}
}

func TestParseTableRefusesMistakes(t *testing.T) {
	good := `{"fallback":"turn","limits":{"turn":{"tenant":{"capacity":1,"per_minute":1},"person":{"capacity":1,"per_minute":1}},"upload":{"tenant":{"capacity":1,"per_minute":1},"person":{"capacity":1,"per_minute":1}},"mcp":{"tenant":{"capacity":1,"per_minute":1},"person":{"capacity":1,"per_minute":1}}},"routes":[%s]}`
	tests := []struct{ name, routes, replace string }{
		{"unknown class", `{"method":"GET","pattern":"/x","class":"admin"}`, ""},
		{"missing method", `{"method":"","pattern":"/x","class":"read"}`, ""},
		{"pattern without a slash", `{"method":"GET","pattern":"x","class":"read"}`, ""},
		{"star in the middle", `{"method":"GET","pattern":"/a/*/b","class":"read"}`, ""},
		{"half a param", `{"method":"GET","pattern":"/a/{b","class":"read"}`, ""},
		{"star glued to text", `{"method":"GET","pattern":"/a/b*","class":"read"}`, ""},
	}
	for _, tc := range tests {
		if _, err := ParseTable([]byte(strings.Replace(good, "%s", tc.routes, 1))); err == nil {
			t.Errorf("%s: accepted", tc.name)
		}
	}
	if _, err := ParseTable([]byte(strings.Replace(good, "%s", `{"method":"GET","pattern":"/x","class":"read"}`, 1))); err != nil {
		t.Fatalf("the control table must parse: %v", err)
	}
	bad := map[string]string{
		"read fallback":      strings.Replace(strings.Replace(good, "%s", "", 1), `"fallback":"turn"`, `"fallback":"read"`, 1),
		"limits for read":    strings.Replace(strings.Replace(good, "%s", "", 1), `"limits":{`, `"limits":{"read":{"tenant":{"capacity":1,"per_minute":1},"person":{"capacity":1,"per_minute":1}},`, 1),
		"zero capacity":      strings.Replace(strings.Replace(good, "%s", "", 1), `"capacity":1,"per_minute":1}},"upload"`, `"capacity":0,"per_minute":1}},"upload"`, 1),
		"unknown field":      strings.Replace(strings.Replace(good, "%s", "", 1), `{"fallback"`, `{"fallbak":"x","fallback"`, 1),
		"missing mcp limits": `{"fallback":"turn","limits":{"turn":{"tenant":{"capacity":1,"per_minute":1},"person":{"capacity":1,"per_minute":1}},"upload":{"tenant":{"capacity":1,"per_minute":1},"person":{"capacity":1,"per_minute":1}}},"routes":[]}`,
		"not json":           `{`,
	}
	for name, doc := range bad {
		if _, err := ParseTable([]byte(doc)); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func TestFirstMatchWins(t *testing.T) {
	doc := `{"fallback":"turn","limits":{"turn":{"tenant":{"capacity":1,"per_minute":1},"person":{"capacity":1,"per_minute":1}},"upload":{"tenant":{"capacity":1,"per_minute":1},"person":{"capacity":1,"per_minute":1}},"mcp":{"tenant":{"capacity":1,"per_minute":1},"person":{"capacity":1,"per_minute":1}}},
	"routes":[{"method":"GET","pattern":"/a/special","class":"read"},{"method":"*","pattern":"/a/*","class":"public"}]}`
	tbl, err := ParseTable([]byte(doc))
	if err != nil {
		t.Fatal(err)
	}
	if got, _ := tbl.Classify("GET", "/a/special"); got != Read {
		t.Errorf("the more specific rule listed first must win, got %s", got)
	}
	if got, _ := tbl.Classify("GET", "/a/other"); got != Public {
		t.Errorf("got %s", got)
	}
}
