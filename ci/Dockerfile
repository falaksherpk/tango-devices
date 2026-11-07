# CI image for tango-devices' lint and test jobs (handbook Part 3 Ch23; Part 4 Ch34).
# The runtime comes from the same hash-locked wheels as the .deb (requirements/runtime.txt),
# with pytest (requirements/test.txt) and ruff (requirements/lint.txt), in one venv that
# is first on PATH, so "python3" and "ruff" in .gitlab-ci.yml are the venv's. No apt Tango
# packages: PyTango's wheel bundles cppTango, and the install test (Ch33) runs every device
# suite with only these. Build context: the ci/ and requirements/ directories of one commit;
# the image is tagged with that commit and named by tag in .gitlab-ci.yml.
# Base pinned by manifest-list digest, as in packaging/deb/Containerfile.
FROM docker.io/library/ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3.12 python3.12-venv \
 && rm -rf /var/lib/apt/lists/*

# Every wheel is checked against its hash, none is built from source, and nothing is
# resolved: the three files together are the complete set, which pip check confirms.
COPY requirements/runtime.txt requirements/test.txt requirements/lint.txt /tmp/requirements/
RUN python3.12 -m venv /opt/ci-venv \
 && /opt/ci-venv/bin/python -m pip --disable-pip-version-check --no-cache-dir install --quiet \
      --require-hashes --only-binary :all: --no-deps \
      -r /tmp/requirements/runtime.txt -r /tmp/requirements/test.txt -r /tmp/requirements/lint.txt \
 && /opt/ci-venv/bin/python -m pip --disable-pip-version-check check \
 && rm -r /tmp/requirements
ENV PATH=/opt/ci-venv/bin:$PATH

WORKDIR /builds
