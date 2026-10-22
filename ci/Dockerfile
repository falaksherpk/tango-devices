FROM ubuntu:24.04

# The Tango client side as in beamline-ansible roles/tango_ds (no simulators,
# no debuggers: the tests use DeviceTestContext). No compiler: every pinned
# package below installs from a binary wheel on Python 3.12 (checked, Ch23).
# --no-install-recommends: python3-pip would otherwise pull in build-essential.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    tango-common \
    libtango-tools \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# The same pins as roles/tango_ds/defaults/main.yml (tango_ds_pip_packages),
# plus sardana for sardana_controllers/. Change a version in both, in one go.
RUN pip3 install --no-cache-dir --break-system-packages \
    pytango==10.3.1 \
    pytest==9.1.1 \
    pyserial==3.5 \
    pymodbus==3.15.0 \
    ruff==0.16.5 \
    sardana==3.7.0

WORKDIR /builds
