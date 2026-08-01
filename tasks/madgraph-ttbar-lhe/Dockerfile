# MG5_aMC built natively for the host architecture (no --platform pin), so
# on Apple Silicon the measured task runs without Rosetta emulation.
FROM python:3.11-slim-bookworm

ARG MG5_VERSION=3.5.16
# Launchpad serves 3.5.16 under the 3.7.x series path; override for mirrors.
ARG MG5_URL=https://launchpad.net/mg5amcnlo/3.0/3.7.x/+download/MG5_aMC_v${MG5_VERSION}.tar.gz

RUN apt-get update && apt-get install -y --no-install-recommends \
        gfortran g++ gcc make wget ca-certificates rsync bc zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir six

WORKDIR /opt
RUN wget -q -O mg5.tar.gz "${MG5_URL}" \
    && tar xzf mg5.tar.gz && rm mg5.tar.gz
# tarball unpacks with dots -> underscores (MG5_aMC_v3_5_16)
RUN ln -s /opt/MG5_aMC_v$(echo ${MG5_VERSION} | tr . _) /opt/mg5
ENV PATH="/opt/mg5/bin:${PATH}"

# Build-time warm-up: run a trivial process end-to-end so one-time setup
# (config writes, fortran toolchain checks) is excluded from measured runs,
# and so a broken build fails here rather than mid-measurement. The ttbar
# process's own code generation + compilation stays inside the measured run
# deliberately — it is part of doing the task.
RUN printf 'set automatic_html_opening False\ngenerate e+ e- > mu+ mu-\noutput /tmp/warmup\nlaunch /tmp/warmup\ndone\nset nevents 10\nset iseed 1\ndone\n' > /tmp/warmup.mg5 \
    && mg5_aMC /tmp/warmup.mg5 \
    && rm -rf /tmp/warmup /tmp/warmup.mg5 py.py

WORKDIR /work
ENTRYPOINT []
