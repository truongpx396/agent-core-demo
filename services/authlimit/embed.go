// Package authlimit is only the home of the JSON registries the service embeds. `go:embed` cannot reach up out of its own directory, so
// they are embedded here, at the module root, and read by the packages that own them (internal/errenv; internal/limiter reads routes.json).
//
// They sit at the module root, not inside internal/, on purpose: PR 2's Python tests read them (the error codes the API must also know, and
// the route table the API's real routes must all appear in). The files are the contract; the Go code only reads them.
package authlimit

import _ "embed"

// ErrorCodesJSON is errorcodes.json: every error code the service emits, with its HTTP status and its fixed caller-facing message.
//
//go:embed errorcodes.json
var ErrorCodesJSON []byte
