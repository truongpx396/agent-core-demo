// Package authlimit is only the home of the two JSON registries the service embeds. `go:embed` cannot reach up out of its own directory, so
// they are embedded here, at the module root, and read by the packages that own them (internal/errenv, internal/limiter).
//
// They sit at the module root, not inside internal/, on purpose: PR 2's Python tests read both files (the error codes the API must also
// know, and the route table the API's real routes must all appear in). The files are the contract; the Go code only reads them.
package authlimit

import _ "embed"

// ErrorCodesJSON is errorcodes.json: every error code the service emits, with its HTTP status and its fixed caller-facing message.
//
//go:embed errorcodes.json
var ErrorCodesJSON []byte

// RoutesJSON is routes.json: the default route-class table (which routes are checked, which are limited, which are public).
//
//go:embed routes.json
var RoutesJSON []byte
