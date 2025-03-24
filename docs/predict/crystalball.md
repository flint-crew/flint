# Predicting model visibilities with `crystalball`

`Crystalball` [is a python package](https://github.com/caracal-pipeline/crystalball) that uses `dask` to accelerate the prediction of model visibilities. Since it is a well supported and typed `python` module it is listed as a `flint` dependency.

## Model specification

The model predicted is described by [a text file with a BBS style source representation](https://support.astron.nl/LOFARImagingCookbook/bbs.html). It is this format that is used by the `wsclean --save-source-list` option. A complete descip[tion of the format is available at the above link. A subset example of the format is below (purely for clarity):

```bash
Format = Name, Type, Ra, Dec, I, SpectralIndex, LogarithmicSI, ReferenceFrequency='743990740.7407408', MajorAxis, MinorAxis, Orientation
174748-312315,GAUSSIAN,17:47:48.619992,-31.23.15.20016,0.19411106571231185,[-3.806350913533059,-4.135453173684361],true,743990740.7407408,68.5999984741211,68.5999984741211,59.79999923706055,
```

`crystalball` is designed specifically to use the model created by `wsclean -save-source-list`, which uses this `bbs` model style.

## Implementation details

`crystalball` uses `dask` to accelerate the prediction of the model visibilities. Under the hood `dask-ms` reads in chunks of a measurement set appropriately sized for the available memory pool. Subsequently the prediction process may be spread across CPUs using the chunked `dask` mappings. The abstraction model used by `dask` also is easily parallelisable across nodes provided an appropriate `dask` cluster has been configured. Thankfully, this is the case in how `prefect` is being used.

The more cores (or dask workers) available in the `dask` cluster the faster the model prediction will be. The individual compute resources can be small (e.g. 2 CPUs and 8GB per `dask-worker`) but through extreme horizontal scale (upwards of 1000 `dask-worker` instances) the model prediction with `crystalball` can be quicker than `addmodel`. On systems such as `SLURM` this resource configuration may be very easy to schedule.

## Accessing via the CLI

The primary entry point for the visibility prediction with `addmodel` in `flint` is with `flint_addmodel`:

```{argparse}
:ref: flint.predict.crystalball.get_parser
:prog: flint_crystalball
```
