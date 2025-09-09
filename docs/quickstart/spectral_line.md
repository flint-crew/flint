(subtractcube)=
# Spectral line and time-step imaging

`flint` has the beginnings of a spectral line workflow that operates against continuum-subtracted visibilities. This mode requires that continuum imaging has been performed using `wsclean`'s `--save-source-list` option, which outputs a text file describing each clean component constructed by `wsclean` and their parameterisation (e.g. location and spatial/spectral shape).

This workflow will first generate model visibilities at the spectral resolution of the data (note that `wsclean` will generate model visibilites that are constant of sub-band intervals). There are two ways to do this:

- using `addmodel` from the `calibrate` container
- using the `crystalball` python package

When using the `addmodel` approach a sub-flow is created, allowing a set of different computing resources to be described. `addmodel` using threads to accelerate compute, which it does extremely well. However it is not able to spread across nodes to achieve higher throughput. By specifying a different `dask` cluster compute profile a large set of resources (e.g. more CPUs, longer wall times) may be specified, thereby speeding up the model prediction.

Alternatively, we also include an approach that uses a [the `crystalball` `python` package](https://github.com/caracal-pipeline/crystalball).  `crystalball` uses `dask` to achieve parallelism. Since `flint` configures `prefect` to use `dask` as its compute backend, `crystalball` is able to use the same infrastructure. This allows the model prediction process to seamlessly scale across many nodes. For most intents and purposes this `crystalball` approach should be preferred.

### Crystalball dask configuration

The best way to achieve throughput when using `crystalball` is to spawn many workers with a small compute footprint. With such a configuration a batch based system (e.g. `slurm`) can still allocate workers even when it is congested, and the distributed nature of `dask` can be leveraged. In practise we found that the following settings (set in the `subtract_cube_pipeline`) were able to improve the stability of the distributed cluster:

```
# These improve the stability of the distributed dask cluster, particularly around
# the usage of crystalball prediction
dask.config.set({"distributed.comm.retry.count": 20})
dask.config.set({"distributed.comm.timeouts.connect": 30})
dask.config.set({"distributed.worker.memory.terminate": False})
```

`Dask` allows these settings to be configured in a number of ways, including via environment variables. In the future this alternative approach may be used, but for the moment they are hardcoded directly in that work flow.

## Imaging axis

The `subtract_cube_pipeline` can image along both the channel (spectral) and scan (temporal) axis. By default the imaging is done per-channel, and only one can be performed within a single execution of this workflow.

Similarly, the imaging is performed at the native resolution of the dimension in the measurement set.

## Achieving parallelism

After a set of model visibilities have been predicted for each measurement set, the result of the continuum subtract leaves what should be noise across all channels (of course, sharp spectral features should also remain). Since there is no benefit to multi-frequency synthesis imaging, each individual channel may be image in isolation from one another. With this in mind the general approach is to configure `dask` to allocate many workers that each individually require  a small set of compute resources.

A field image is produced at each channel by a separate `linmos` process invocation. That is to say, if there are 288 channels in the collection of measurement sets, there will be 288 separate  `linmos` processes throughout the flow. Once all field images have been proceduced they are concatenated together (in an asynchronous and memory efficient way) into a single FITS cube. See the [fitscube python package for more information](https://github.com/alecthomson/fitscube).

### Processes vs Threads

A `dask-worker` can be assigned a set of cpu cores that are to be used throughout computation. Further, a `dask-worker` can be configured to execute work using either a `ProcessPool` or a `ThreadPool`, each with different types of advantages and disadvantages. Should many external compiled applications be called upong (as is the case throughout the self-calibration and imaging pipeline), a process backed work pool is advised:

```yaml
cluster_kwargs:
    cores: 1
    processes: 1
    job_cpu: 4
    name: 'flint-worker'
    memory: "32GB"
```

Here, 4 cores and 32GB of RAM are provided to each dask worker, and each dask worker can only be executing a single task at a time. Should those tasks call out to a compiled application that can take advantage of multiple-cores (e.g. `wsclean`) they will be used.

Alternatively for the `subtract_cube_pipeline` the following may be useful (or even preferred) if `crystalball` is being used:

```yaml
cluster_kwargs:
    cores: 4
    processes: 1
    job_cpu: 4
    name: 'flint-worker'
    memory: "32GB"
```

This will configure each `dask-worker` to use 4 threads at a time, meaning 4 tasks can be executing concurrently. In the case of `crystalball` this modification may lower inter-process communication load (include network read/writes between nodes) as `dask` can now consider data locality as a component of the scheduling. Should this be a driving consideration in you configuration? No. Maybe. Unlikely. But it is still something to be aware of.

In the context of the `subtract_cube_pipeline` context, should multiple `wsclean` and `linmos` be tasks be invoked those applications would be sharing the 4 assigned cores and 32GB memory pool. Over-subscribing in this way, provided out-of-memory limits are not reached, may improve overall utilisation of the compute allocation as these applications rarely use all available cores in a sustained sense.

```{admonition} Caution
:class: caution

Should configurations like `$MEMDIR` be sure to consider overlapping data and side-effects. For instance, should `wsclean -tmp-dir $SLURM_TEMP_SPACE` be used, be mindful of the clean up operation when the `wsclean` task finishes. Should `$SLURM_TEMP_SPACE` (a imagined variable for this discussion) be used concurrently by many threads in a single `dask-worker` the final move from a single thread will effect data in others still working. Hence, consider `-tmp-dir $SLURM_TEMP_SPACE/$FLINT_UUID`, where `$FLINT_UUID` will trigger a unique identifier to be appended, thus avoiding side-effects.
```

## Output data

The principle result of this workshow is a spectral cube of the observed field at the native spectral resolution of the input collection of measurement sets (including the corresponding weight map produced by `linmos`). Intermediary files created throughout the workflow are deleted once they are no longer needede in order to preserve disk space.

## Accessing via the CLI

The primary entry point for the continuum subtraction and spectral imaging pipeline in `flint` is the `flint_flow_subtract_cube_pipeline`:

```{argparse}
:ref: flint.prefect.flows.subtract_cube_pipeline.get_parser
:prog: flint_flow_subtract_cube_pipeline
```

## The `SubtractFieldOptions` class

Embedded below is the `flint` `Options` class used to drive the `flint_flow_subtract_cube_pipeline` workflow. Input values are validated by `pydantic` to ensure they are appropriately typed.

```{literalinclude}  ../../flint/options.py
:pyobject: SubtractFieldOptions
```

## The `AddModelSubtractFieldOptions` class

Embedded below is the `flint` `Options` class used to drive the `flint_flow_subtract_cube_pipeline` workflow. Input values are validated by `pydantic` to ensure they are appropriately typed.

```{literalinclude}  ../../flint/options.py
:pyobject: AddModelSubtractFieldOptions
```
