# Image for native worker calls: the model CLI runs here, on a copy of the code at /work.
# The CLI binary, its per-call login and the packet's Python environment are mounted at run time;
# nothing secret is baked in. Built once by scripts/coordination/native.py build-image.
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git ripgrep python3 procps less patch diffutils findutils \
 && rm -rf /var/lib/apt/lists/*
RUN git config --system safe.directory '*'
