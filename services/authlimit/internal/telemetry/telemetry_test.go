package telemetry

import (
	"bytes"
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
)

func collect(t *testing.T, reader *sdkmetric.ManualReader) map[string]metricdata.Metrics {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatal(err)
	}
	out := map[string]metricdata.Metrics{}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			out[m.Name] = m
		}
	}
	return out
}

func newTestMetrics(t *testing.T) (*Metrics, *sdkmetric.ManualReader) {
	t.Helper()
	reader := sdkmetric.NewManualReader()
	m, err := New(sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader)).Meter("test"))
	if err != nil {
		t.Fatal(err)
	}
	return m, reader
}

func TestEveryMetricIsRegisteredUnderItsLiteralName(t *testing.T) {
	m, reader := newTestMetrics(t)
	m.Request("turn", "allowed", "", time.Millisecond)
	m.KeyRefresh(true)
	m.StoreError(StoreLimiter)
	m.SetKeysAge(time.Second)
	m.SetRevocationAge(time.Second)
	m.SetSigningKeyAge(time.Second)
	m.SetReady(true)

	got := collect(t, reader)
	for _, name := range []string{NameRequests, NameRequestDuration, NameKeyRefresh, NameKeysAge, NameRevocationAge, NameStoreErrors, NameReady, NameSigningKeyAge} {
		if _, ok := got[name]; !ok {
			t.Errorf("metric %s was not exported", name)
		}
		if !strings.HasPrefix(name, "agent_authlimit_") {
			t.Errorf("%s does not carry the agent_authlimit_ prefix alerts.yml and its drift test rely on", name)
		}
	}
}

func TestNoMetricCarriesAnIdentifyingLabel(t *testing.T) {
	// A tenant, principal or token as a label is an identifier in the metrics backend and unbounded cardinality.
	m, reader := newTestMetrics(t)
	m.Request("turn", "refused", "bad_signature", time.Millisecond)
	m.Request("mcp", "limited", "", time.Millisecond)
	m.KeyRefresh(false)
	m.StoreError(StoreRevocation)

	allowed := map[string]bool{AttrRouteClass: true, AttrOutcome: true, AttrReason: true, AttrResult: true, AttrStore: true}
	for name, metric := range collect(t, reader) {
		switch data := metric.Data.(type) {
		case metricdata.Sum[int64]:
			for _, dp := range data.DataPoints {
				for _, kv := range dp.Attributes.ToSlice() {
					if !allowed[string(kv.Key)] {
						t.Errorf("%s carries label %q", name, kv.Key)
					}
				}
			}
		case metricdata.Histogram[float64]:
			for _, dp := range data.DataPoints {
				for _, kv := range dp.Attributes.ToSlice() {
					if !allowed[string(kv.Key)] || kv.Key == AttrReason {
						t.Errorf("%s carries label %q (the histogram stays low-cardinality: class and outcome only)", name, kv.Key)
					}
				}
			}
		}
	}
}

func TestHistogramHasExplicitBuckets(t *testing.T) {
	m, reader := newTestMetrics(t)
	m.Request("turn", "allowed", "", 3*time.Millisecond)
	h, ok := collect(t, reader)[NameRequestDuration].Data.(metricdata.Histogram[float64])
	if !ok || len(h.DataPoints) == 0 {
		t.Fatal("no histogram data")
	}
	if len(h.DataPoints[0].Bounds) != len(latencyBuckets) {
		t.Fatalf("bounds = %v", h.DataPoints[0].Bounds)
	}
}

func TestNilMetricsIsSafe(t *testing.T) {
	var m *Metrics
	m.Request("turn", "allowed", "", time.Second)
	m.KeyRefresh(true)
	m.StoreError(StoreLimiter)
	m.SetKeysAge(time.Second)
	m.SetRevocationAge(time.Second)
	m.SetSigningKeyAge(time.Second)
	m.SetReady(false)
}

func TestSetupWithoutAnEndpointExportsNothing(t *testing.T) {
	m, shutdown, err := Setup(context.Background(), "", "authlimit-1")
	if err != nil {
		t.Fatal(err)
	}
	m.Request("turn", "allowed", "", time.Millisecond)
	if err := shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestLoggerIsJSONWithTheReplicaAndHonoursTheLevel(t *testing.T) {
	var buf bytes.Buffer
	log := NewLogger(&buf, "warn", "authlimit-2")
	log.Info("dropped")
	log.Warn("kept", "route_class", "turn", "reason", "bad_signature")
	lines := strings.Split(strings.TrimSpace(buf.String()), "\n")
	if len(lines) != 1 {
		t.Fatalf("expected one line at level warn, got %q", buf.String())
	}
	var rec map[string]any
	if err := json.Unmarshal([]byte(lines[0]), &rec); err != nil {
		t.Fatal(err)
	}
	if rec["replica"] != "authlimit-2" || rec["msg"] != "kept" || rec["route_class"] != "turn" {
		t.Fatalf("record = %v", rec)
	}
}
