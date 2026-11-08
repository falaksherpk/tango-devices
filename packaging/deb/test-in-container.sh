#!/usr/bin/env bash
# The install test (handbook Part 4, Ch33). Runs as root in a fresh ubuntu:24.04 container
# started by packaging/deb/test-deb.sh: the .debs on /debs, the committed tests/ and
# requirements/ in /test. Installs both packages as apt would, checks that the runtime is
# exactly the lock, and runs the device suites against the installed code.
set -euo pipefail

apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /debs/*.deb
dpkg-query -W -f '${Package} ${Version} ${Architecture}\n' 'beamline-tango-*'
id tango

# The runtime holds exactly the lock's 17 distributions plus this package, and no pip. This
# package's version is the installed .deb's, never a literal: the "1.0.0" written here
# failed 1.0.1's install test (handbook Part 4, Ch35). Native X.Y.Z versions only, as
# ci/check-versions.py keeps them, so dpkg's and Python's spellings are the same.
/opt/beamline-tango/bin/python3 -I - /test/requirements/runtime.txt \
  "$(dpkg-query -W -f '${Version}' beamline-tango-devices)" << 'PY'
import importlib.metadata as md, re, sys
lock = {}
for line in open(sys.argv[1]):
    m = re.match(r"^([A-Za-z0-9._-]+)==(\S+)", line)
    if m:
        lock[re.sub(r"[-_.]+", "-", m.group(1)).lower()] = m.group(2)
lock["beamline-tango-devices"] = sys.argv[2]
have = {re.sub(r"[-_.]+", "-", d.metadata["Name"]).lower(): d.version for d in md.distributions()}
if have != lock:
    print("runtime differs from the lock:", sorted(set(have.items()) ^ set(lock.items())))
    sys.exit(1)
print(f"runtime == lock: {len(have)} distributions, no pip")
PY

# pytest from its own lock, outside the runtime; then the suites as CI orders them. The pip
# that installs it comes from a throwaway venv, which needs python3.12-venv: test tooling,
# installed only after the runtime check above, never a dependency of the packages.
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends python3.12-venv
python3.12 -m venv /tmp/fetch
/tmp/fetch/bin/python3 -m pip --disable-pip-version-check --no-cache-dir install --quiet \
  --target /tmp/testdeps --no-deps --require-hashes --only-binary :all: -r /test/requirements/test.txt
cd /test
rc=0
PYTHONPATH=/tmp/testdeps /opt/beamline-tango/bin/python3 -m pytest -p no:cacheprovider \
  tests/vacuum/ tests/safety/ tests/magnet/ tests/camera/ tests/beam/ 2>&1 | tee /tmp/pytest.log || rc=$?
if grep -n "Fatal Python error" /tmp/pytest.log; then
  echo "test-in-container: crash trace(s) in the log above (pytest exit $rc)"
  exit 1
fi
echo "pytest exit $rc"
exit "$rc"
