FROM condaforge/miniforge3
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV DEBIAN_FRONTEND=noninteractive

# Install apptainer
WORKDIR /tmp
RUN apt-get update && apt-get install -y wget rsync && \
    DEBIAN_FRONTEND=noninteractive  apt-get install -y --no-install-recommends tzdata && \
    wget https://github.com/apptainer/apptainer/releases/download/v1.4.0/apptainer_1.4.0_amd64.deb && \
    apt-get install -y ./apptainer_1.4.0_amd64.deb && \
    rm ./apptainer_1.4.0_amd64.deb && \
    apt-get autoremove -y && apt-get clean -y && rm -rf /var/lib/apt/lists/*


# Create conda environment
WORKDIR /root
# setting libsqlite for https://github.com/PrefectHQ/prefect/issues/17186
RUN conda install -c conda-forge python=3.12 libsqlite=3.46.1 boost casacore compilers
# Make RUN commands use the new environment:
SHELL ["conda", "run", "-n", "base", "/bin/bash", "-c"]
RUN conda clean --all --yes

# Install flint
COPY ../ /flint
WORKDIR /flint
RUN uv pip install -e .[casa] --compile-bytecode

WORKDIR /root
