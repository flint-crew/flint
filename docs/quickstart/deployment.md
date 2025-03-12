(deployment)=
# Deployment

The `flint` python package is logically divided into two parts:

- A set of pure python functions and classes to do isolated units of work
- A specialised `flint.prefect` sub-module to coordinate tasks and workflows

The distinction is important as it would allow the transition to a different workflow orchestration tool should our requirements change.
Presently we are using a `python` module named `prefect` to designed and implement workflows within `flint`, but effort has been made
towards keeping these two components separated should a new workflow manager been needed.

Although logically separated, these components are packaged together. Simply installing `flint` installs all required tooling. Deploying
should therefore be straight forward and as simple as a `pip` command.

(prefect)=
## Prefect orchestration

[`Prefect`](https://github.com/PrefectHQ/prefect) is a workflow orchestration framework for building data pipelines in python. A pipeline attempts to control the flow of data between tasks, and manage the potentially complex set of dependencies that exist between different stages. The goal of `prefect` is to facilitate this with as little code as possible while representing the work in a form that is distinct from the compute environment and the `python` functions themselves. By appropriately managing this the workflow in of itself is remarkably scalble with little dependence on compute platforms.

### The strategy of using Prefect

Prefect provides an [installation and quickstart guide](https://docs.prefect.io/v3/get-started/install) on their official website.
There information is provided that not only more thoroughly explains their framework, but also gives sets on how to tap into their
cloud platform (more below) or how to set up a self-hosted `prefect.server` instance. This information will be more complete (and
correct) than the brief notes here.

Workflow observability is one of the key features offered by a `prefect` server. Any workflow that is
executed by the `prefect` framework is registered in an relational database. The `prefect` software allows
provides an interactive webpage that draws from this database, allowing an operator to inspect all workflows,
inspect results, and schedule new triggers.

The `prefect` cloud platform offers a managed solution of such a `prefect` server instance.

### A simple deployment

As a user do you need to also setup a relational database server and `prefect` server instance? No. Should
you run a `prefect` enabled workflow without these, `prefect` will automatically use a short lived set of
database and server processes to manage the workflow. This comes at a scalability cost though. Large workflows
with many concurrent sets of independent workers may overwhelm the default shortlived set of services.

### Deploying your own prefect server

If you want to have a scalable self-host solution there are two components that need to be established:

- a `postgres` SQL server
- a `prefect` server

These can (and should for most cases) be set up on the same machine. A helper script to start `postgres`
through `apptainer`:

```bash

# Real Pirates put in their own secure postgres passwords and usernames!!
export POSTGRES_PASS=PUT_YOUR_PASSWORD_HERE
export POSTGRES_USER=PUT_YOUR_USER_NAME_HERE
export POSTGRES_ADDR="your.machine.name.or.ip"

export POSTGRES_DB=prefect # you can change this to whatever you want
export POSTGRES_SCRATCH=$(realpath $(pwd))
export POSTGRES_PORT=5432 # consider changing this if there is already an attached service

export APPTAINER_BINDPATH="$POSTGRES_SCRATCH" # sometimes necessary should defaults be broken

if [[ ! -e "${POSTGRES_SCRATCH}/pgdata" ]]; then
    echo "Creating pgdata for the postgres server operation"
    mkdir pgdata
fi

if [[ ! -e postgres_latest.sif ]]
then
    echo "Downloading the latest postgres docker container"
    apptainer pull docker://postgres
fi

APPTAINERENV_POSTGRES_PASSWORD="$POSTGRES_PASS" APPTAINERENV_POSTGRES_DB="$POSTGRES_DB" APPTAINERENV_PGDATA="$POSTGRES_SCRATCH/pgdata" APPTAINER_POSTGRES_PORT="$POSTGRES_PORT" \
        apptainer run --cleanenv --bind "$POSTGRES_SCRATCH":/var postgres_latest.sif \
        -p $POSTGRES_PORT \
        -c max_connections=9124 \
        -c wal_level=minimal \
        -c synchronous_commit=off \
        -c wal_buffers=64MB \
        -c checkpoint_timeout=60min \
        -c checkpoint_completion_target=0.9 \
        -c max_wal_size=8GB \
        -c min_wal_size=2GB \
        -c wal_writer_delay=1ms \
        -c commit_delay=100 \
        -c commit_siblings=10 \
        -c max_wal_senders=0 \
        -c shared_buffers=16GB \
        -c work_mem=64MB \
        -c maintenance_work_mem=256MB \
        -c effective_cache_size=48GB
```

Note that you should change `POSTGRES_PASS`, `POSTGRES_USER` and `POSTGRES_ADDR` appropriately.

Placing this into an appropriate shell script and running should start a `postgres` database server.

Next, assuming you have already installed `prefect` in an environment on your server, the following
should start it:

```bash
export POSTGRES_PASS=PUT_YOUR_PASSWORD_HERE
export POSTGRES_USER=PUT_YOUR_USER_NAME_HERE
export POSTGRES_ADDR=127.0.0.1 # if running on the same machine don't change

export POSTGRES_DB=prefect # you can change this to whatever you want
export POSTGRES_PORT=5432 # consider changing this if there is already an attached service

# These instruct prefect where the postgres server is and where it should expect
# to send prefect restful api messages.
export PREFECT_API_URL="http://${POSTGRES_ADDR}:4200/api"
export PREFECT_SERVER_API_HOST="127.0.0.1"
export PREFECT_API_DATABASE_CONNECTION_URL="postgresql+asyncpg://$POSTGRES_USER:$POSTGRES_PASS@$POSTGRES_ADDR:5432/$POSTGRES_DB"

# These attempt to make prefect more scalable and robust to many, many workers
export WEB_CONCURRENCY=12
export PREFECT_SQLALCHEMY_POOL_SIZE=75
export PREFECT_SQLALCHEMY_MAX_OVERFLOW=150
export PREFECT_API_DATABASE_TIMEOUT=80
export PREFECT_API_DATABASE_CONNECTION_TIMEOUT=90
export PREFECT_SERVER_CSRF_PROTECTION_ENABLED=False
export PREFECT_HOME="$(pwd)/prefect"


python -m uvicorn \
    --app-dir "$(dirname $(which python))/../lib/python3.*/site-packages" \
    --factory prefect.server.api.server:create_app \
    --host 0.0.0.0 \
    --port 4200 \
    --timeout-keep-alive 10 \
    --limit-max-requests 4096 \
    --timeout-graceful-shutdown 7200

```

A couple of points should be notes. The most important is that the set of `postgres` credentials described here need to
match those described in the `postgres` start script above. If they do not the `prefect` server can not authenticate and
commit information to the database.

Additionally, we are invoking `uvicorn` directly in order to access a larger suite of scalability options that are
nor exposed via the `prefect server` CLI interface. A consequence of this is that the `--app-dir` needs to specify
the location of the site-packages of the appropriate python environment. The current value attempts to work this
out in place. For proper robustness this should be changed.

Provided these two services start without throwing an error you should not be able to visit port 4200 of your
server in a web browser to access the `prefect` web page.

## Running a `prefect` flow

Should you want to run a flow that is registered against this `prefect` instance you will need to set the following environment variable in your workflow scripts:

```bash
export APIURL=http://${YOUR_MACHINE_ADDRESS}:4200/api
```

where you put an appropriate IP address or fully qualified hostname of the server running the `prefect` service
as outline above. The `prefect` client that will be running the workflow (e.g. the main `python` process) will communicate with the RESTful API endpoint established above. Should you be using the `prefect` cloud there will be an `API` token that should be set instead. Refer to the official set of `prefect` docs for further information.

Throughout `flint` we have configured `prefect` to use `dask` as its compute backend. `prefect` sits on top of `dask` to provide an additional set of event based triggers and workflow observatibility, but under the hood distributed task execution relies on `dask` infrastructure. The `dask.distributed` schedular is responsible for coordinating the execution of tasks among a workflow, and it may be configured to run on many different platforms. Typically, most `flint` workflows to-date have been run on HPC systems controlled by `SLURM`. Through the `dask_jobqueue` package `flint` can be configured to execute its workflows seamlessly on such a platform strictly through a single configuration file.  Below is an example of a YAML file that could be provided to `flint` to establish a set of workers using a `SLURMCluster` object.

```yaml
cluster_class: "dask_jobqueue.SLURMCluster"
cluster_kwargs:
    cores: 1
    processes: 1
    job_cpu: 8
    name: 'flint-worker'
    memory: "128GB"
    walltime: '0-24:00:00'
    job_extra_directives:
      - '--qos express'
      - '--no-requeue'
    # interface for the workers
    interface: "ib0"
    log_directory: 'flint_logs'
    job_script_prologue:
        - 'module load singularity'
        - 'unset SINGULARITY_BINDPATH'
        - "export OMP_NUM_THREADS=${SLURM_CPUS_ON_NODE}"
    local_directory: $LOCALDIR
    silence_logs: 'info'
adapt_kwargs:
    minimum: 1
    maximum: 38
```

Providing a path to a YAML file will configure `flint` to:

- use `SLURM` to acquire compute resources for each dask work
- each dask worker will be allocated 8 CPU cores and 128GB memory
- set a wall time of 24 hours
- adaptively scale the number of concurrent dask workers from 1 worker up to 38 workers (done so as demanded by the number of available tasks to run)

Here a `dask-worker` refers to an agent established by `dask` that carries out work. Here work refers to some task that has been been created and registered by `prefect` onto the `dask` work graph. Each `dask-worker` that is created is persistent so long as there is work. They each can carry out many distinct and isolated tasks. The distributed `dask` cluster is responsible for this coordination, and is capable to anticipanting issues around data locaclity (e.g. in memory data structures/results that would need to bne transferred) when allocating work.

See `dask_jobqueue.SLURMCluster` for a complete list of available keyword arguments.

Note that there there are many other `dask` cluster types for a variety of platforms. Redeploying to a new platform should be straightforward if there exists a `dask` cluster interface for it.
