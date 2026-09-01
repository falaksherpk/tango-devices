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
    pytango \
    pytest \
    pyserial \
    redis \
    ruff==0.16.5

WORKDIR /builds
