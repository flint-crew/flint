# Installation

The `flint` module itself is built in pure Python and can be installed using `pip`. We highly recommend using [uv](https://docs.astral.sh/uv/) for speedy installation.

For compiled tools, `flint` uses `singularity` containers. Users must install `singularity` or `apptainer` on their system for these to work. If given the choice, install `apptainer`, which is the community developed and maintained fork of `singularity`.

## Containers

We separately maintain a set of required containers. Pre-built containers are hosted on [DockerHub](https://hub.docker.com/r/alecthomson/flint-containers/tags), and the underlying build scripts can be found over in the [`flint-containers` repository](https://github.com/flint-crew/flint-containers).

With `flint` installed, you can use the `flint_containers` CLI to download or check the required containers:

```bash
flint_containers -h
# usage: flint_containers [-h] {list,download,verify} ...

# Download and manage Flint containers

# positional arguments:
#   {list,download,verify}
#     list                List the containers that are known to Flint
#     download            Pull each of the known containers
#     verify              Pull each of the known containers

# options:
#   -h, --help            show this help message and exit
```

```bash
flint_containers download -h
# usage: flint_containers download [-h] container_directory

# positional arguments:
#   container_directory  Location to download containers to

# options:
#   -h, --help           show this help message and exit
```

In a future release it is planned that tasks or processes that require a container
will internally resolve them automatically. For the moment though it is expected
that the user provides the appropriate set of paths through CLI keyword arguments.

## Installing casacore

Provided an appropriate environment, installinf `flint` should be as simple as a
`pip install`. However, on some systems there are interactions with `casacore` and building
`python-casacore` appropriately. Issues have been noted when interacting with
large measurement sets across components with different `casacore` versions.
This seems to happen even across container boundaries (i.e. different versions
in containers might play a role). The exact cause is not at all understood, but
it appears to be related to the version of `python-casacore`, `numpy` and
whether pre-built wheels are used. In recent releases this problem does appear
to have been eliminated, almost certainly in upstream dependencies.

In practise it might be easier to leverage `conda` to install the appropriate
`boost` and `casacore` libraries.

We have split out the `pip` dependencies that rely on `python-casacore`. These
can be installed by running from within thg `git clone` `flint` repository folder:

```bash
pip install '.[casa]'
```

A helpful script below may be of use.

```bash
BRANCH="main" # replace this with appropriate branch or tag
DIR="flint_${BRANCH}"
PYVERSION="3.12"

mkdir "${DIR}" || exit
cd "${DIR}" || exit


git clone git@github.com:tjgalvin/flint.git && \
        cd flint && \
        git checkout "${BRANCH}"

conda create -y  -n "${DIR}" python="${PYVERSION}" &&  \
        source /home/$(whoami)/.bashrc && \
        conda activate "${DIR}" && \
        conda install -y -c conda-forge boost casacore && \
        PIP_NO_BINARY="python-casacore" pip install -e '.[casa]'
```

This should set up an appropriate environment that is compatible with the
containers currently being used. Do note though that we are not attempting
to install or configure `apptainer`.
