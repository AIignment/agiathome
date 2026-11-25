FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    PYTHONHASHSEED=0 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      quantum-espresso \
      python3 \
      python3-pip \
      python3-numpy \
      python3-scipy && \
    pip3 install --no-cache-dir --break-system-packages pymatgen ase && \
    rm -rf /var/lib/apt/lists/*

COPY verify-materials.py /usr/local/bin/verify-materials.py
RUN chmod +x /usr/local/bin/verify-materials.py

VOLUME ["/tmp"]
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/verify-materials.py"]
