# Sky-model calibration

```{admonition} Caution
:class: caution

Sky-model calibration is still a work in progress and should not be relied upon. It uses an idealised primary beam response (i.e. no holography) when predicting the apparent brightness of sources towards a direction. This level of of precision may not be suited for some purposes (e.g. bandpass calibration).
```

`flint` provides basic functionality that attempts to create a sky-model that could be used to calibrate against. By using a reference catalogue that describes the positions of a set of 2D Gaussian
components, their shape and spectral variance, the sky as the telescope sees it
can be predicted. The subsequent model, provided the estimation is correct, can then be
used to to predict model visibilities and perform subsequent calibration.

This functionality was the genesis of the `flint` codebase, but it has not been incorporated
into any of the calibration workflow procedures. It outputs a text file based on the name of the input measurement set and specified desired output format. That is to say it does not produce predicted model visibilities in of itself, but ddepending on context can be provided to something that could predict and/or solve.

## Basic Overview

There are three principle components that are required to create a local sky-model:

1 - A basic catalogue at a known frequency. Where possible it should also describe the intrinsic spectral behaviour of each source component,
2 - The desired frequency and direction of the sky where the model is being estimated, and
3 - A ddescription of the primary beam of the instrument.

`flint` integrated functionality to interact with a set of known referencce catalogues. These catalogues describe the sky as a set of two-dimensional Gaussian components at a particular frequency. The apparent brightness of components in a nominated reference catalogue are estimated by extracting the frequency and pointing direction information encoded in a measurement set and estimating the primary beam direction. `flint`)currently supports `gaussian`, `sincsquared` and `airy` type responses.

Once the apparent brightness of a source is estimated across all nominated frequencies (extracted from an input measurement set) `flint` will fit a low-order polynomial model to the resulting spectra. The constrained modeel parameters are subsequently encoded in the output models. Model visibilities can then be produced and insertede as a `MODEL_DATA` column in the nominated measurement set for subsequent calibration.

```{admonition} Caution
:class: caution

Although there are three response patterns known (`Gaussian`, `SincSquared` and `Airy`) only the `Gaussian` response is used when creating the sky-model through
`flint.sky_model.create_sky_model`.
```

### Fitting the spectral shape

`flint` will fit a third order polynomial in log space when constrain the apparent spectrum.

The exact functional form is presented below:

```{literalinclude}  ../../flint/sky_model.py
:pyobject: curved_power_law
```


### Holography is not currently suupported

The ASKAP observatory performs regular holography measurements to characterise the primary beam response of each of the electonically formed beams. The output response pattern is known to now be entirely consistent with analytical descriptions of idealised beam responses. Presently `flint` does not use the holography to estimate the apparent brightness of sources.

Therefore it may be unwise to use such a sky-model as the basis of bandpass calibration. In time this type of prediction may be inforporated into `flint`, but presently it remains as a future to-do. Contributions are welcome.

## Output model types

`flint` will write out the local sky model in a variety of formats.

### `calibrate` and `wsclean --save-source-list` style

This is a self-descriptive format format that supports point sources and two-dimensional Gaussians. This format is also known as the `BBS` style.

Positions are in the J2000 epoch. The nominal frequenci is encoded in Hz as part of the `ReferenceFrequency` column title. Fluxes are recorded in Jy. Major and minor axis sizes of Gaussian components are in units of arcsecond, with the PA in degrees.

The nominal intensity column `I` is measured at the reference frequency. A list polynomial co-efficients in logarithmic space may be provided in the `SpectralIndex` column.

This file format is what `wsclean` produces via its `--save-source-list` option. Below is an example of the header and first row of the sky-model. This style of sky-model may be used as an input for `crystalball` and `addmodel`, as {ref}`described in the subtract cube imaging workflow <subtractcube>`.

```bash
Format = Name, Type, Ra, Dec, I, SpectralIndex, LogarithmicSI, ReferenceFrequency='743990740.7407408', MajorAxis, MinorAxis, Orientation
174748-312315,GAUSSIAN,17:47:48.619992,-31.23.15.20016,0.19411106571231185,[-3.806350913533059,-4.135453173684361],true,743990740.7407408,68.5999984741211,68.5999984741211,59.79999923706055,
```

A more thorough and complete version of [this BBS style format may be accessed here](https://support.astron.nl/LOFARImagingCookbook/bbs.html).

### `hyperdrive` style

[`hyperdrive` is extremely efficient calibration utility developed for the MWA](https://github.com/MWATelescope/mwa_hyperdrive). Although ASKAP is obviously not MWA, under some conditions it is possibly to use `hyperdrive` to calibrate ASKAP measurement sets. There are some technical considerations that sometimes prohibit this, but when it works it is extremely speedy.

There are a number of formats that `hyperdrive` supports when specifying a sky-model. In `flint` we choose to output sources in a `yaml` style format, when each component is described as a record in a fairly flat schema. [See their description page for more information.](https://mwatelescope.github.io/mwa_hyperdrive/defs/source_list_hyperdrive.html)

Below is an example of how to describe a single source.

```yaml
174748-312315:
- comp_type:
    gaussian:
      maj: 68.5999984741211
      min: 18.5
      pa: 59.79999923706055
  dec: -31.3875556
  flux_type:
    curved_power_law:
      fd:
        freq: 743990740.7407408
        i: 0.19411106571231185
      q: -4.135453173684361
      si: -3.806350913533059
  ra: 266.9525833
```

### DS9 region file

`flint` may also be configured to output a simple DS9 region file that can be used as an overlauf. Of course, no brightness information is recorded, and we refer the reader to `DS9` documentation for more information on the region style format.

The first source is listed as an example.

```bash
# DS9 region file
fk5
ellipse(266.952583,-31.387556,68.599998,18.500000,149.800003) # color=red dash=1
```

## Accessing via the CLI

The primary entry point for the skymodel program in `flint` is the `flint_skymodel`:

```{argparse}
:ref: flint.sky_model.get_parser
:prog: flint_skymodel
```
