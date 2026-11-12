# Predicting model visibilities with `addmodel`

The `addmodel` program, which is packaged in the larger `calibrate` container, can be used to predict model visibilities into an existing measurement set.

## Model specification

The model to predict is described by [a text file with a BBS-style source representation](https://support.astron.nl/LOFARImagingCookbook/bbs.html). This is the format written by the `wsclean -save-source-list` option. A complete description of the format is available at the above link. A short example of the format is below:

```text
Format = Name, Type, Ra, Dec, I, SpectralIndex, LogarithmicSI, ReferenceFrequency='743990740.7407408', MajorAxis, MinorAxis, Orientation
174748-312315,GAUSSIAN,17:47:48.619992,-31.23.15.20016,0.19411106571231185,[-3.806350913533059,-4.135453173684361],true,743990740.7407408,68.5999984741211,68.5999984741211,59.79999923706055,
```

## Implementation details

The `addmodel` application itself is very fast and memory efficient. When using it, be aware of how it scales. More model sources to predict, or more channels, times and baselines to predict across, will increase the computation required. Much of this is linear: doubling the number of sources to predict will double the computation required. Memory usage is fairly constant, and `addmodel` is generally intelligent in how it chunks the data it predicts across.

As a general rule of thumb, give `addmodel` as many CPUs as possible, as it will happily spread the work across many threads.

## Accessing via the CLI

The primary entry point for visibility prediction with `addmodel` in `flint` is `flint_addmodel`:

```{argparse}
:ref: flint.predict.addmodel.get_parser
:prog: flint_addmodel
```
