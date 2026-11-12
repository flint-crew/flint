# Predicting model visibilities with `crystalball`

`crystalball` [is a Python package](https://github.com/caracal-pipeline/crystalball) that uses `dask` to accelerate the prediction of model visibilities. Since it is a well-supported, typed Python module, it is listed as a `flint` dependency.

## Model specification

The model to predict is described by [a text file with a BBS-style source representation](https://support.astron.nl/LOFARImagingCookbook/bbs.html). This is the format written by the `wsclean -save-source-list` option. A complete description of the format is available at the above link. A short example of the format is below:

```text
Format = Name, Type, Ra, Dec, I, SpectralIndex, LogarithmicSI, ReferenceFrequency='743990740.7407408', MajorAxis, MinorAxis, Orientation
174748-312315,GAUSSIAN,17:47:48.619992,-31.23.15.20016,0.19411106571231185,[-3.806350913533059,-4.135453173684361],true,743990740.7407408,68.5999984741211,68.5999984741211,59.79999923706055,
```

`crystalball` is designed specifically to use the model created by `wsclean -save-source-list`, which uses this BBS model style.

## Implementation details

`crystalball` uses `dask` to accelerate the prediction of the model visibilities. Under the hood, `dask-ms` reads the measurement set in chunks sized to fit the available memory. The prediction is then spread across CPUs using the chunked `dask` mappings. The same `dask` abstraction also parallelises easily across nodes, provided an appropriate `dask` cluster has been configured. Thankfully, this is how `prefect` is used in `flint`.

The more cores (or `dask` workers) available in the `dask` cluster, the faster the model prediction will be. The individual compute resources can be small (e.g. 2 CPUs and 8 GB per `dask-worker`), but with extreme horizontal scaling (upwards of 1000 `dask-worker` instances) the model prediction with `crystalball` can be quicker than `addmodel`. On clusters managed by `SLURM`, this resource configuration can be very easy to schedule.

## Stability

Under some conditions, the `dask.distributed` scheduler managing the `crystalball` prediction can stall, at which point all processing essentially stops. The work resumes only after the scheduler recognises that a worker has died, which on `SLURM` often happens only when the job's time limit is reached.

Through experimentation, we found the following `dask` configuration settings to be useful:

```python
# These improve the stability of the distributed dask cluster, particularly around
# the usage of crystalball prediction
dask.config.set({"distributed.comm.retry.count": 20})
dask.config.set({"distributed.comm.timeouts.connect": 30})
dask.config.set({"distributed.worker.memory.terminate": False})
```

These are set when using the `subtract_cube_pipeline`, but are not set by the `flint_crystalball` program. In that case, consider setting them through the environment (see the [Dask configuration documentation](https://docs.dask.org/en/stable/configuration.html)).

## Accessing via the CLI

The primary entry point for visibility prediction with `crystalball` in `flint` is `flint_crystalball`:

```{argparse}
:ref: flint.predict.crystalball.get_parser
:prog: flint_crystalball
```
