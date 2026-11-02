#!/usr/bin/env bash
# The build itself (handbook Part 4, Ch33). Runs as root in a fresh builder container
# started by packaging/deb/build-deb.sh: the committed tree in /build/src, the wheel cache
# on /wheels, the output directory on /out.
set -euo pipefail

cd /build/src
apt-get update -qq
apt-get build-dep -y -qq --no-install-recommends /build/src

# The runtime's wheels: fetched into the cache, and every one checked against the lock's
# hashes, cached or not. The build itself then installs offline from the cache.
python3.12 -m venv /tmp/fetch
/tmp/fetch/bin/python3 -m pip --disable-pip-version-check --no-cache-dir download --quiet \
  --no-deps --require-hashes --only-binary :all: --dest /wheels -r requirements/runtime.txt

# The venv is built in place at its final path (debian/rules); the unprivileged user
# builds (Rules-Requires-Root: no) and runs lintian; root copies the results out.
install -d -o ubuntu -g ubuntu /opt/beamline-tango
chown -R ubuntu:ubuntu /build
runuser -u ubuntu -- env BEAMLINE_WHEELHOUSE=/wheels dpkg-buildpackage -b -us -uc
cd /build
runuser -u ubuntu -- lintian --fail-on error --info ./*.changes
cp ./*.deb ./*.buildinfo ./*.changes /out/
