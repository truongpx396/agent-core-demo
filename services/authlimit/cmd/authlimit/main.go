// Command authlimit is the edge authentication and rate-limit service (specs/018-authlimit-edge-auth).
//
//	authlimit serve                                  run the service (the default)
//	authlimit healthcheck [--ready]                  exit 0 if this replica answers /healthz (or /readyz); the container healthcheck
//	authlimit keys init|rotate|list --dir D          manage the internal signing keys
//	authlimit revoke --tenant T --sub S              void every token issued to a person up to now, on every replica
//	authlimit revoke --list                          show the current revocations
//
// The image has no shell and no curl, which is why the healthcheck is a subcommand of the one binary.
package main

import (
	"fmt"
	"os"
)

func main() {
	args := os.Args[1:]
	cmd := "serve"
	if len(args) > 0 && len(args[0]) > 0 && args[0][0] != '-' {
		cmd, args = args[0], args[1:]
	}
	var code int
	switch cmd {
	case "serve":
		code = serve(args)
	case "healthcheck":
		code = healthcheck(args)
	case "keys":
		code = keys(args)
	case "revoke":
		code = revokeCmd(args)
	case "help", "-h", "--help":
		fmt.Fprint(os.Stderr, "usage: authlimit [serve | healthcheck [--ready] | keys init|rotate|list --dir D | revoke --tenant T --sub S | revoke --list]\n")
	default:
		fmt.Fprintf(os.Stderr, "authlimit: unknown command %q\n", cmd)
		code = 2
	}
	os.Exit(code)
}
