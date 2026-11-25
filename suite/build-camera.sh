#!/bin/sh
# Build the pinned upstream server with IP-specific listeners and a pre-bound discovery socket.
set -eu
out=$1
case "$out" in /*) ;; *) out="$PWD/$out";; esac
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
temp=$(mktemp -d)
trap 'rm -rf "$temp"' EXIT
cd "$here/upstream"
go mod download github.com/Aiaid/onvif-proxy
src=$(go list -mod=readonly -m -f '{{.Dir}}' github.com/Aiaid/onvif-proxy)
cp -R "$src/." "$temp/"
chmod -R u+w "$temp"
cd "$temp"
patch -p1 < "$here/listeners.patch"
patch -p1 < "$here/events.patch"
patch -p1 < "$here/snapshot.patch"
cp "$here/snapshot_auth.go" "$here/snapshot_auth_test.go" internal/onvif/
cp "$here/events.go" internal/onvif/events.go
cp "$here/events_test.go" internal/onvif/events_test.go
GOOS=$(go env GOHOSTOS) GOARCH=$(go env GOHOSTARCH) go test -mod=readonly ./internal/onvif ./internal/soap ./internal/discovery
CGO_ENABLED=0 GOOS=linux go build -mod=readonly -trimpath -o "$out" ./cmd/onvif-proxy
