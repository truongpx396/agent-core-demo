// Package telemetry is the service's logging and metrics.
//
// Logs are structured JSON on stdout carrying metadata only: the route class, the outcome, a reason word, the replica. Never a header, a
// token, a claim value or an exception's text (constitution Principle V and VI; threat model T15). Metrics go out over OTLP/HTTP to the
// same collector the app and the MCP gateway use, so `alerts.yml` and the dashboards treat them alike; with no endpoint configured they
// are not exported at all, which keeps `go test` and a bare `docker run` quiet.
//
// Every method on Metrics is safe on a nil receiver, so a package under test can be handed nil instead of a fake.
package telemetry

import (
	"context"
	"io"
	"log/slog"
	"math"
	"strings"
	"sync/atomic"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/metric/noop"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
)

// Metrics holds the instruments. Gauges are observed from atomics, set by whoever owns the value, so a scrape never calls into the cache.
type Metrics struct {
	requests    metric.Int64Counter
	duration    metric.Float64Histogram
	keyRefresh  metric.Int64Counter
	storeErrors metric.Int64Counter

	keysAge, revocationAge, signingKeyAge atomic.Uint64 // float64 bits
	ready                                 atomic.Int64
}

// New registers the instruments on a meter.
func New(meter metric.Meter) (*Metrics, error) {
	m := &Metrics{}
	var err error
	if m.requests, err = meter.Int64Counter(NameRequests,
		metric.WithDescription("Decisions by route class, outcome and reason (refusals carry a fixed reason word, never request data).")); err != nil {
		return nil, err
	}
	if m.duration, err = meter.Float64Histogram(NameRequestDuration, metric.WithUnit("s"),
		metric.WithDescription("Time to decide one request."), metric.WithExplicitBucketBoundaries(latencyBuckets...)); err != nil {
		return nil, err
	}
	if m.keyRefresh, err = meter.Int64Counter(NameKeyRefresh,
		metric.WithDescription("Attempts to fetch the authorization server's signing keys, by result.")); err != nil {
		return nil, err
	}
	if m.storeErrors, err = meter.Int64Counter(NameStoreErrors,
		metric.WithDescription("Failed or too-slow calls to the shared store, by which use of it (limiter or revocation).")); err != nil {
		return nil, err
	}
	for name, desc := range map[string]struct {
		src  *atomic.Uint64
		text string
	}{
		NameKeysAge:       {&m.keysAge, "Seconds since the authorization server's keys were last fetched successfully."},
		NameRevocationAge: {&m.revocationAge, "Seconds since this replica last loaded the revocation list successfully."},
		NameSigningKeyAge: {&m.signingKeyAge, "Seconds since the current internal signing key became current."},
	} {
		src := desc.src
		if _, err = meter.Float64ObservableGauge(name, metric.WithUnit("s"), metric.WithDescription(desc.text),
			metric.WithFloat64Callback(func(_ context.Context, o metric.Float64Observer) error {
				o.Observe(math.Float64frombits(src.Load()))
				return nil
			})); err != nil {
			return nil, err
		}
	}
	if _, err = meter.Int64ObservableGauge(NameReady,
		metric.WithDescription("1 if this replica can decide requests, 0 if not. One series per replica."),
		metric.WithInt64Callback(func(_ context.Context, o metric.Int64Observer) error {
			o.Observe(m.ready.Load())
			return nil
		})); err != nil {
		return nil, err
	}
	return m, nil
}

// Setup builds the Metrics for the process. With no endpoint it uses a no-op meter. The returned function flushes and stops the exporter.
// The exporter reads OTEL_EXPORTER_OTLP_ENDPOINT itself (the standard variable), so the same environment configures it as the app's.
func Setup(ctx context.Context, endpoint, instance string) (*Metrics, func(context.Context) error, error) {
	if endpoint == "" {
		m, err := New(noop.NewMeterProvider().Meter("authlimit"))
		return m, func(context.Context) error { return nil }, err
	}
	exp, err := otlpmetrichttp.New(ctx)
	if err != nil {
		return nil, nil, err
	}
	res, err := resource.New(ctx, resource.WithAttributes(
		attribute.String("service.name", "authlimit"),
		attribute.String("service.instance.id", instance),
	))
	if err != nil {
		return nil, nil, err
	}
	provider := sdkmetric.NewMeterProvider(
		sdkmetric.WithReader(sdkmetric.NewPeriodicReader(exp, sdkmetric.WithInterval(15*time.Second))),
		sdkmetric.WithResource(res),
	)
	m, err := New(provider.Meter("authlimit"))
	if err != nil {
		return nil, nil, err
	}
	return m, provider.Shutdown, nil
}

// Request records one decision. `outcome` and `reason` are the reason package's words.
func (m *Metrics) Request(routeClass, outcome, reason string, took time.Duration) {
	if m == nil {
		return
	}
	attrs := []attribute.KeyValue{attribute.String(AttrRouteClass, routeClass), attribute.String(AttrOutcome, outcome), attribute.String(AttrReason, reason)}
	m.requests.Add(context.Background(), 1, metric.WithAttributes(attrs...))
	m.duration.Record(context.Background(), took.Seconds(), metric.WithAttributes(attrs[0], attrs[1]))
}

// KeyRefresh records one attempt to fetch the authorization server's keys.
func (m *Metrics) KeyRefresh(ok bool) {
	if m == nil {
		return
	}
	result := ResultError
	if ok {
		result = ResultOK
	}
	m.keyRefresh.Add(context.Background(), 1, metric.WithAttributes(attribute.String(AttrResult, result)))
}

// StoreError records a failed or too-slow call to the shared store. `store` is StoreLimiter or StoreRevocation.
func (m *Metrics) StoreError(store string) {
	if m == nil {
		return
	}
	m.storeErrors.Add(context.Background(), 1, metric.WithAttributes(attribute.String(AttrStore, store)))
}

// SetKeysAge, SetRevocationAge and SetSigningKeyAge update the age gauges.
func (m *Metrics) SetKeysAge(d time.Duration) {
	setAge(m, func(x *Metrics) *atomic.Uint64 { return &x.keysAge }, d)
}
func (m *Metrics) SetRevocationAge(d time.Duration) {
	setAge(m, func(x *Metrics) *atomic.Uint64 { return &x.revocationAge }, d)
}
func (m *Metrics) SetSigningKeyAge(d time.Duration) {
	setAge(m, func(x *Metrics) *atomic.Uint64 { return &x.signingKeyAge }, d)
}

func setAge(m *Metrics, field func(*Metrics) *atomic.Uint64, d time.Duration) {
	if m == nil {
		return
	}
	field(m).Store(math.Float64bits(d.Seconds()))
}

// SetReady sets the readiness gauge.
func (m *Metrics) SetReady(ready bool) {
	if m == nil {
		return
	}
	var v int64
	if ready {
		v = 1
	}
	m.ready.Store(v)
}

// NewLogger returns the JSON logger. `replica` is on every line so two replicas' lines can be told apart.
func NewLogger(w io.Writer, level, replica string) *slog.Logger {
	var lv slog.Level
	switch strings.ToLower(level) {
	case "debug":
		lv = slog.LevelDebug
	case "warn":
		lv = slog.LevelWarn
	case "error":
		lv = slog.LevelError
	default:
		lv = slog.LevelInfo
	}
	return slog.New(slog.NewJSONHandler(w, &slog.HandlerOptions{Level: lv})).With("replica", replica)
}
