FROM ubuntu:24.04

# Same packages as roles/tango_ds/tasks/main.yml, minus the lab-only
# protocol simulators (socat/redis-server) which Part 1's tests don't need.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    tango-common \
    libtango-tools \
    build-essential \
    libtango-dev \
    cmake \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --break-system-packages \
    pytango==10.3.1 \
    pytest==9.1.1 \
    pyserial==3.5 \
    redis==8.1.0 \
    pymodbus==3.15.0 \
    ruff==0.16.5 \
    sardana==3.7.0

WORKDIR /builds
