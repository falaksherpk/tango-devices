#!/usr/bin/env bash
# Build beamline-tango-devices' two .debs from the committed HEAD in a clean, throwaway
# container (handbook Part 4, Ch33; the pattern of Ch15).
# Usage: packaging/deb/build-deb.sh OUTDIR
# Needs the builder image:
#   podman build -t localhost/beamline-tango-deb-builder:noble packaging/deb
# The runtime's wheels are cached in ${XDG_CACHE_HOME:-~/.cache}/beamline-tango-devices/wheels
# and checked against requirements/runtime.txt's hashes at every build.
set -euo pipefail

image=localhost/beamline-tango-deb-builder:noble
out=${1:?usage: $0 OUTDIR}
cache=${XDG_CACHE_HOME:-$HOME/.cache}/beamline-tango-devices/wheels

repo=$(git rev-parse --show-toplevel)
cd "$repo"
if [[ -n $(git status --porcelain --untracked-files=no) ]]; then
  echo "refusing: tracked files have uncommitted changes; the build uses HEAD only" >&2
  exit 1
fi
if ! podman image exists "$image"; then
  echo "refusing: builder image $image not found" >&2
  exit 1
fi
mkdir -p "$out" "$cache"
out=$(realpath "$out")
if compgen -G "$out/*.deb" >/dev/null; then
  echo "refusing: $out already contains .deb files" >&2
  exit 1
fi

echo "=== building $(git rev-parse --short HEAD) in $image -> $out"
# git archive sends exactly the committed tree (no untracked files, no build leftovers).
git archive --format=tar HEAD |
  podman run --rm -i -v "$out:/out" -v "$cache:/wheels" "$image" bash -euo pipefail -c '
    mkdir /build/src
    tar -x -C /build/src
    bash /build/src/packaging/deb/build-in-container.sh
  '
echo "=== results"
ls -l "$out"
sha256sum "$out"/*.deb
