// Package api is the Go side of the service's OpenAPI document (../../openapi.json): the types of every body it sends and the server
// interface its routes are registered from, GENERATED, so the document is the one place a route or a field is declared. api.gen.go is
// never edited; change openapi.json and run `make authlimit-generate`. CI regenerates it and fails on a difference.
//
// The generator's version is pinned in the directive below (not in go.mod: a `tool` entry would pull the generator's whole module graph
// into this service's go.sum, and it is a build tool, not a dependency). Bump it here, regenerate, and review the diff.
//
// Two things the generator would do that this service must not, and why the document is written around them:
//   - A declared header parameter is bound by the generated wrapper, which answers a repeated one with a plain-text 400 before the handler
//     runs. /v1/check must instead read X-Forwarded-Method and X-Forwarded-Uri raw (a repeated one is an unclean path, never public), so
//     they are described in the operation's text and not declared as parameters.
//   - `const` is not modelled (the field would become interface{}), and rewriting it as a one-value `enum` would change the contract in
//     oasdiff's eyes (a "breaking" change to every response property). So the document keeps `const` and says the Go type with the
//     `x-go-type: string` extension, which no client reads.
package api

//go:generate go run github.com/oapi-codegen/oapi-codegen/v2/cmd/oapi-codegen@v2.8.1 -config oapi-codegen.yaml ../../openapi.json
