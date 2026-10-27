// Command echo is the stand-in upstream for the smoke check (spec 018): it answers every request with the method, the path and every
// header it received, as JSON. Behind Caddy's forward_auth it shows exactly what a real API would have been given: whether the identity
// header arrived, whether the caller's Authorization header was removed, whether a forged header survived.
//
// It is a test fixture. It is built into the service's image only so the smoke needs no second toolchain; it is not started by anything
// but the development compose profile, and it holds no secrets.
package main

import (
	"encoding/json"
	"log"
	"net/http"
	"sort"
	"time"
)

func main() {
	mux := http.NewServeMux()
	health := func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte(`{"status":"ok"}`)) }
	// Only the compose healthcheck's own path answers plainly. Every other path, /health and /health/ready included, is ECHOED, so the smoke
	// check can tell that a public API route really reached the upstream (and with which headers).
	mux.HandleFunc("/healthz", health)
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		headers := map[string][]string{}
		for k, v := range r.Header {
			headers[k] = v
		}
		keys := make([]string, 0, len(headers))
		for k := range headers {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"method": r.Method, "path": r.URL.RequestURI(), "headers": headers, "header_names": keys})
	})
	srv := &http.Server{Addr: ":8000", Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	log.Fatal(srv.ListenAndServe())
}
