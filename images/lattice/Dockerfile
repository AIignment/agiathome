FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    VECLIB_MAXIMUM_THREADS=1 \
    BLIS_NUM_THREADS=1 \
    PYTHONHASHSEED=0 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-numpy && \
    rm -rf /var/lib/apt/lists/*

COPY verify-lattice.py /usr/local/bin/verify-lattice.py
RUN chmod +x /usr/local/bin/verify-lattice.py

VOLUME ["/tmp"]
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/verify-lattice.py"]
