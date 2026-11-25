#!/bin/sh
# Rebuild the Nest bridge bundled in the suite image.
set -eu
out=$1
case "$out" in /*) ;; *) out="$PWD/$out";; esac
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
temp=$(mktemp -d)
trap 'rm -rf "$temp"' EXIT
cd "$temp"
git init -q
git fetch -q --depth=1 https://github.com/ceb3/nest-to-ONVIF.git 5b68d1b206cab6af594511ab8529abc05ef71644
git checkout -q --detach FETCH_HEAD
git apply "$here/bridge-rtp.patch"
GOOS=$(go env GOHOSTOS) GOARCH=$(go env GOHOSTARCH) go test -mod=readonly ./internal/media ./internal/session
CGO_ENABLED=0 GOOS=linux go build -mod=readonly -buildvcs=false -trimpath -ldflags="-s -w" -o "$out" ./cmd/nest-bridge
