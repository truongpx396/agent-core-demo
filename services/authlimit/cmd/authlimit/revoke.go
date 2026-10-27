package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"time"

	"authlimit/internal/config"
	"authlimit/internal/revoke"
)

// revokeCmd implements `authlimit revoke`. It writes to the same Redis the replicas poll, so it works from any replica's container; it
// needs the replica's environment (the Redis address and the key prefix), which `docker compose exec` already has.
func revokeCmd(args []string) int {
	fs := flag.NewFlagSet("revoke", flag.ContinueOnError)
	tenant := fs.String("tenant", "", "the person's tenant (the token's organization)")
	sub := fs.String("sub", "", "the person's id (the token's subject)")
	list := fs.Bool("list", false, "show the current revocations instead of adding one")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	cfg, err := config.Load(os.Getenv)
	if err != nil {
		return fail("configuration:\n%v", err)
	}
	rdb, err := redisClient(cfg)
	if err != nil {
		return fail("%v", err)
	}
	defer rdb.Close()
	timeout := cfg.RedisTimeout
	if timeout < 2*time.Second {
		timeout = 2 * time.Second // an operator command may wait a little longer than a request may
	}
	l := revoke.New(rdb, revoke.Config{
		Prefix: cfg.KeyPrefix, PollEvery: cfg.RevocationPoll, MaxStale: cfg.RevocationMaxStaleness,
		Policy: revoke.Policy(cfg.RevocationUnavailable), Timeout: timeout,
	}, nil, nil, nil)
	ctx := context.Background()

	if *list {
		entries, err := l.Entries(ctx)
		if err != nil {
			return fail("revoke --list: %v", err)
		}
		if len(entries) == 0 {
			fmt.Println("no revocations")
		}
		for _, e := range entries {
			fmt.Printf("%s/%s  tokens issued up to %s are void\n", e.Tenant, e.Principal, e.At.UTC().Format(time.RFC3339))
		}
		return 0
	}
	if *tenant == "" || *sub == "" {
		return fail("revoke: --tenant and --sub are both required")
	}
	now := time.Now()
	if err := l.Add(ctx, *tenant, *sub, now, cfg.MaxTokenLifetime); err != nil {
		return fail("%v", err)
	}
	fmt.Printf("revoked %s/%s: every token issued to them up to %s is void on every replica within %s; a new sign-in works from the next second\n",
		*tenant, *sub, now.UTC().Format(time.RFC3339), cfg.RevocationPoll+time.Second)
	return 0
}
