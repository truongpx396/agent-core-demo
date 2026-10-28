package main

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/redis/go-redis/v9"

	"authlimit"
	"authlimit/internal/config"
	"authlimit/internal/extjwt"
	"authlimit/internal/identity"
	"authlimit/internal/limiter"
	"authlimit/internal/revoke"
	"authlimit/internal/server"
	"authlimit/internal/telemetry"
)

// fail prints a start-up problem and returns the exit code. It never prints a value: the Redis URL can hold a password.
func fail(format string, a ...any) int {
	fmt.Fprintf(os.Stderr, "authlimit: "+format+"\n", a...)
	return 2
}

// loadTable reads the route table: the file named by AUTHLIMIT_ROUTES_FILE, else the one embedded in the binary.
func loadTable(path string) (*limiter.Table, error) {
	data := authlimit.RoutesJSON
	if path != "" {
		b, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("AUTHLIMIT_ROUTES_FILE: %w", err)
		}
		data = b
	}
	return limiter.ParseTable(data)
}

// redisClient builds the client for the limiter and the revocation list. Every call carries its own deadline; the client's timeouts are a
// second line, and retries are off so one slow call is not three.
func redisClient(cfg config.Config) (*redis.Client, error) {
	opts, err := redis.ParseURL(cfg.RedisURL)
	if err != nil {
		return nil, errors.New("AUTHLIMIT_REDIS_URL is not a usable Redis URL") // the parser's error can quote the URL, which can hold a password
	}
	opts.DialTimeout, opts.ReadTimeout, opts.WriteTimeout = cfg.RedisTimeout, cfg.RedisTimeout, cfg.RedisTimeout
	opts.MaxRetries = 0
	return redis.NewClient(opts), nil
}

func serve(args []string) int {
	_ = args
	cfg, err := config.Load(os.Getenv)
	if err != nil {
		return fail("configuration:\n%v", err)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	return run(ctx, cfg)
}

// run is the service: build everything from a validated configuration, serve until ctx ends, then drain. It is separate from serve so a
// test can run the real wiring (the thing no unit test of one package touches) on a free port with a cancellable context.
func run(ctx context.Context, cfg config.Config) int {
	table, err := loadTable(cfg.RoutesFile)
	if err != nil {
		return fail("%v", err)
	}
	if table.HasClass(limiter.MCP) && cfg.MCPMetadataURLTemplate == "" {
		return fail("AUTHLIMIT_MCP_METADATA_URL_TEMPLATE: is required when the route table has MCP routes (a revoked person's 401 must carry the discovery URL)")
	}

	log := telemetry.NewLogger(os.Stdout, cfg.LogLevel, cfg.InstanceID)
	metrics, shutdownMetrics, err := telemetry.Setup(ctx, cfg.OTLPEndpoint, cfg.InstanceID)
	if err != nil {
		return fail("metrics: %v", err)
	}

	// A replica with nothing to sign with must not start: it could verify tokens and never answer, and look healthy while doing it.
	keyset, err := identity.LoadKeyset(cfg.KeysDir, cfg.KeysLoosePerms, nil)
	if err != nil {
		return fail("%v", err)
	}
	rdb, err := redisClient(cfg)
	if err != nil {
		return fail("%v", err)
	}
	defer rdb.Close()

	keys := extjwt.NewKeyCache(extjwt.KeyCacheConfig{
		URL: cfg.JWKSURL, TTL: cfg.JWKSCacheTTL, MinRefresh: cfg.JWKSMinRefresh, Grace: cfg.JWKSStaleGrace, Timeout: cfg.JWKSFetchTimeout,
	}, nil, nil, metrics, log)
	revocations := revoke.New(rdb, revoke.Config{
		Prefix: cfg.KeyPrefix, PollEvery: cfg.RevocationPoll, MaxStale: cfg.RevocationMaxStaleness,
		Policy: revoke.Policy(cfg.RevocationUnavailable), Timeout: cfg.RedisTimeout,
	}, nil, metrics, log)

	srv := server.New(server.Deps{
		Cfg: cfg, Table: table,
		Verifier: extjwt.NewVerifier(extjwt.Settings{
			Issuer: cfg.Issuer, Audience: cfg.Audience, RequiredScope: cfg.RequiredScope, TenantClaim: cfg.TenantClaim,
			PrincipalClaim: cfg.PrincipalClaim, MaxLifetime: cfg.MaxTokenLifetime, Leeway: cfg.ClockLeeway,
		}, keys, nil),
		Keys: keys, Keyset: keyset, Minter: identity.NewMinter(keyset, cfg.IdentityTTL, nil),
		Limiter: limiter.NewRedis(rdb, cfg.KeyPrefix, cfg.RedisTimeout, nil), Revoked: revocations,
		Metrics: metrics, Log: log,
	})

	go keys.Run(ctx)
	go revocations.Run(ctx)
	go srv.Watch(ctx)

	httpSrv := srv.HTTPServer(cfg.Listen)
	errc := make(chan error, 1)
	go func() {
		log.Info("listening", "addr", cfg.Listen, "limit_mode", cfg.LimitMode, "revocation_policy", cfg.RevocationUnavailable)
		errc <- httpSrv.ListenAndServe()
	}()

	select {
	case err := <-errc:
		if !errors.Is(err, http.ErrServerClosed) {
			log.Error("server_stopped", "error", err.Error())
			return 1
		}
	case <-ctx.Done():
	}
	// A bounded drain: finish the requests in flight, then stop. A hung connection must not hold a rolling restart.
	drain, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = httpSrv.Shutdown(drain)
	_ = shutdownMetrics(drain)
	log.Info("stopped")
	return 0
}

func healthcheck(args []string) int {
	path := "/healthz"
	for _, a := range args {
		if a == "--ready" {
			path = "/readyz"
		}
	}
	addr := os.Getenv("AUTHLIMIT_LISTEN")
	if addr == "" {
		addr = ":8080"
	}
	host, port, err := net.SplitHostPort(addr)
	if err != nil {
		return fail("AUTHLIMIT_LISTEN is not host:port")
	}
	if host == "" || host == "0.0.0.0" || host == "::" {
		host = "127.0.0.1"
	}
	client := &http.Client{Timeout: 2 * time.Second}
	resp, err := client.Get("http://" + net.JoinHostPort(host, port) + path)
	if err != nil {
		return 1
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return 1
	}
	return 0
}
