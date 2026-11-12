# Running flint from its container on Slurm

This page describes how to run `flint` from its published apptainer image on
a Slurm cluster, instead of installing it into a Python environment on the
host. The difficulty is that `flint` uses `dask_jobqueue` to start dask workers,
and that calls `sbatch` and `scancel` from inside the container.

```{admonition} Experimental
:class: warning

This has been tested on one cluster only: Slurm 25.05 with `auth/munge`,
apptainer 1.5 running without setuid, and the `v0.10.0` image. Slurm commands
and nested containers worked there, but a full pipeline has not yet been run
this way. Every site lays out Slurm differently, so expect to adapt the paths
below, and it may not work on your system at all.
```

## Get the image

```bash
apptainer pull flint.sif docker://ghcr.io/flint-crew/flint:{tag}
```

## Check your system

The image includes its own `apptainer`, which `flint` uses to run its tool
containers (`wsclean`, `casa`, ...) from inside the `flint` container. This
needs unprivileged user namespaces and `/dev/fuse` on the compute nodes:

```bash
cat /proc/sys/user/max_user_namespaces   # must be greater than 0
ls -l /dev/fuse                          # must exist and be readable/writable
```

Check that a tool container runs inside the `flint` container. Replace
`<data dir>` with the filesystem that holds your data and containers:

```bash
apptainer exec --bind <data dir> flint.sif \
    apptainer exec <data dir>/path/to/wsclean.sif wsclean --version
```

If the output mentions converting the SIF file to a temporary sandbox, FUSE is
not working. Each tool container will then be unpacked on every call, which is
slow.

## Find your Slurm installation

On the host, run:

```bash
which sbatch
scontrol show config | grep -Ei 'PluginDir|SLURM_CONF|AuthType'
ldd $(which sbatch)
ldd <PluginDir>/auth_munge.so | grep munge
```

These give:

- the directory holding the Slurm binaries;
- the plugin directory, which usually also holds `libslurmfull.so`;
- the location of `slurm.conf`;
- the authentication method. Only `auth/munge` has been tested;
- the host's `libmunge.so.2`, which the munge plugin loads at runtime.

## Binds for Slurm access

Only the process that runs the flow needs these, because it is the one that
submits the worker jobs. In this example, Slurm is installed under
`<slurm prefix>`:

```bash
MUNGE=$(ldd <PluginDir>/auth_munge.so | awk '/libmunge/{print $3}')
SLURM_BINDS=<slurm prefix>,/etc/slurm,/run/munge,/etc/passwd,/etc/group,$MUNGE:/usr/lib/x86_64-linux-gnu/libmunge.so.2
export APPTAINERENV_APPEND_PATH=<directory holding sbatch>
```

- Bind every directory that the commands above pointed to, at the same path
  inside the container. Binaries, plugins and `slurm.conf` might all be under
  one prefix, or spread across several.
- `/etc/passwd` and `/etc/group` are needed because `slurm.conf` names a
  `SlurmUser`, and that user does not exist in the image.
- `libmunge.so.2` is bound into the image's library directory. The image is
  Ubuntu-based, so this is `/usr/lib/x86_64-linux-gnu`.
- `APPTAINERENV_APPEND_PATH` is needed because apptainer does not pass the
  host `PATH` into the container.
- The host's Slurm binaries run against the image's C library, which needs to
  be at least as new as the host's.

Test with:

```bash
apptainer exec --bind $SLURM_BINDS flint.sif squeue --me
apptainer exec --bind $SLURM_BINDS,<data dir> flint.sif bash -c \
    'jid=$(sbatch --parsable --time=00:01:00 --wrap hostname) && echo $jid && scancel $jid'
```

Add your usual options, such as `--account`, to the `sbatch` call.

## Flow job

Run your usual flow command inside the image:

```bash
module load apptainer   # or however apptainer is provided on your system
unset APPTAINER_BINDPATH SINGULARITY_BINDPATH
apptainer exec --bind <data dir>,$SLURM_BINDS flint.sif flint_flow_<...> <usual args>
```

The `unset` line is only needed if your site sets these variables. A site's
default binds can clash with the ones given here, and they are passed on to the
nested tool containers.

## Workers

`dask_jobqueue` writes `<python> -m distributed.cli.dask_worker ...` into each
worker's job script. By default `<python>` is the flow process's own Python,
which is a path inside the container that does not exist on the bare compute
node. Set it in the cluster configuration so workers start inside the image:

```yaml
cluster_kwargs:
  python: "apptainer exec --bind <data dir> /path/to/flint.sif python"
  job_script_prologue:
    - "module load apptainer"
    - "unset APPTAINER_BINDPATH"
    - "unset SINGULARITY_BINDPATH"
```

Workers do not need the Slurm binds. Apptainer shares the host network, so
`interface` settings such as `ib0` still work.

## Messages you can ignore

- `fuse2fs not found` and `gocryptfs not found`. These are only needed for
  EXT3 overlays and encrypted images.
- `destination is already in the mount point list`. The site's default binds
  overlap with yours. Unset `APPTAINER_BINDPATH` to silence it.
- An extra `nobody` group in `id` inside the container. That is a host group
  with no counterpart in the container.
