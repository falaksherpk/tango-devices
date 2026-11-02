#!/usr/bin/env bash
# Install beamline-tango-devices' .debs into a fresh ubuntu:24.04 container and run the
# device test suites against the installed code (handbook Part 4, Ch33).
# Usage: packaging/deb/test-deb.sh DEBDIR
set -euo pipefail

base=docker.io/library/ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55
debs=${1:?usage: $0 DEBDIR}
debs=$(realpath "$debs")
compgen -G "$debs/beamline-tango-devices_*_amd64.deb" >/dev/null || { echo "refusing: no beamline-tango-devices .deb in $debs" >&2; exit 1; }
compgen -G "$debs/beamline-tango-simulators_*_all.deb" >/dev/null || { echo "refusing: no beamline-tango-simulators .deb in $debs" >&2; exit 1; }

repo=$(git rev-parse --show-toplevel)
cd "$repo"
echo "=== testing $(cd "$debs" && sha256sum ./*.deb | sed 's|  ./| |') with tests/ of $(git rev-parse --short HEAD)"
git archive --format=tar HEAD tests requirements packaging/deb/test-in-container.sh |
  podman run --rm -i -v "$debs:/debs:ro" "$base" bash -euo pipefail -c '
    mkdir /test
    tar -x -C /test
    bash /test/packaging/deb/test-in-container.sh
  '
