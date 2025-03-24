# Predicting model visibilities with `addmodel`

The `addmodel` program that is packaged in the larger `calibrate` container can be used to predict model visibilities into an existing measurement set.

## Model specification

The model predicted is described by [a text file with a BBS style source representation](https://support.astron.nl/LOFARImagingCookbook/bbs.html). It is this format that is used by the `wsclean --save-source-list` option. A complete descip[tion of the format is available at the above link. A subset example of the format is below (purely for clarity):

```bash
Format = Name, Type, Ra, Dec, I, SpectralIndex, LogarithmicSI, ReferenceFrequency='743990740.7407408', MajorAxis, MinorAxis, Orientation
174748-312315,GAUSSIAN,17:47:48.619992,-31.23.15.20016,0.19411106571231185,[-3.806350913533059,-4.135453173684361],true,743990740.7407408,68.5999984741211,68.5999984741211,59.79999923706055,
```

## Implementation details

The `addmodel` application itself is very speedy and memory efficient. When using it be aware of how it scales. A larger set of model sources to predict or a larger set of frequency/time/baselines to predict across will increase the computation required. Much of this is linear -- doubling the number of sources to predict will double the computation required. Memory usage is fairly constant, and `addmodel` is generally intelligent in how it chunks the data to predict across.

The general rule of thumb is to acquire as many CPUs as possible when running `addmodel` as it will happily vectorise across many threads.

## Accessing via the CLI

The primary entry point for the visibility prediction with `addmodel` in `flint` is with `flint_addmodel`:

```{argparse}
:ref: flint.predict.addmodel.get_parser
:prog: flint_addmodel
```
