FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    ELAN_HOME=/opt/elan \
    PATH="/opt/elan/bin:${PATH}"

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl git python3 && \
    rm -rf /var/lib/apt/lists/* && \
    curl -sSfL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh | \
      sh -s -- -y --default-toolchain leanprover/lean4:v4.14.0 --no-modify-path && \
    lean --version

COPY verify-lean.sh /usr/local/bin/verify-lean.sh
RUN chmod +x /usr/local/bin/verify-lean.sh

VOLUME ["/tmp"]
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/verify-lean.sh"]
