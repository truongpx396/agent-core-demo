package main

import (
	"flag"
	"fmt"
	"os"
	"time"

	"authlimit/internal/identity"
)

// keys implements `authlimit keys init|rotate|list`. None of them prints key material: only key ids, states and times.
func keys(args []string) int {
	if len(args) == 0 {
		return fail("keys: say init, rotate or list")
	}
	sub, rest := args[0], args[1:]
	fs := flag.NewFlagSet("keys "+sub, flag.ContinueOnError)
	dir := fs.String("dir", os.Getenv("AUTHLIMIT_KEYS_DIR"), "the key directory (default: AUTHLIMIT_KEYS_DIR)")
	overlap := fs.Duration("overlap", 10*time.Minute, "rotate: how long before the new key signs, and again before the old one retires")
	if err := fs.Parse(rest); err != nil {
		return 2
	}
	if *dir == "" {
		return fail("keys %s: --dir (or AUTHLIMIT_KEYS_DIR) is required", sub)
	}
	now := time.Now()
	switch sub {
	case "init":
		kid, err := identity.InitKeyset(*dir, now)
		if err != nil {
			return fail("keys init: %v", err)
		}
		fmt.Printf("created the key directory with key %s (current now)\n", kid)
	case "rotate":
		kid, err := identity.RotateKeyset(*dir, now, *overlap)
		if err != nil {
			return fail("keys rotate: %v", err)
		}
		fmt.Printf("added key %s: published now, signs from %s, and the key it replaces is served until %s\n",
			kid, now.Add(*overlap).UTC().Format(time.RFC3339), now.Add(2**overlap).UTC().Format(time.RFC3339))
	case "list":
		statuses, err := identity.ListKeyset(*dir, now)
		if err != nil {
			return fail("keys list: %v", err)
		}
		for _, s := range statuses {
			retire := "never"
			if s.RetireAt != 0 {
				retire = time.Unix(s.RetireAt, 0).UTC().Format(time.RFC3339)
			}
			fmt.Printf("%-34s %-10s publish %s  use %s  retire %s\n", s.KID, s.State,
				time.Unix(s.PublishAt, 0).UTC().Format(time.RFC3339), time.Unix(s.UseFrom, 0).UTC().Format(time.RFC3339), retire)
		}
	default:
		return fail("keys: unknown subcommand %q (init, rotate or list)", sub)
	}
	return 0
}
