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

There are thre principle components that are required to create a local sky-model:

1 - A basic catalogue at a known frequency. Where possible it should also describe the intrinsic spectral behavour of each source component, 
2 - The desired frequency and direction of the sky where the model is being estimated, and
3 - A ddescription of the primary beam of the instrument.

`flint` integrated functionality to interact with a set of known referencce catalogues. These catalogues describe the sky as a set of two-dimensional Gaussian components at a partcular frequency. The apparent brightness of components in a nominated reference catalogue are estimated by extracting the frequency and pointing direction information encoded in a measurement set and estimating the primary beam direction. `flint`)currently supports `gaussian`, `sincsquared` and `airy` type responses. 

Once the apparent brightness of a source is estimated across all nominated frequencies (extracted from an input measurement set) `flint` will fit a low-order polynomial model to the resulting spectra. The constrained modeel parametes are subsequently encoded in the output models. Model visibilities can then be produced and insertede as a `MODEL_DATA` column in the nominated measurement set for subsequent calibration. 

### Holography is not currently suupported

The ASKAP observatory performs regular holography measurements to characterise the primary beam response of each of the electonically formed beams. The output response pattern is known to now be entirely consistent with analytical descriptions of idealised beam responses. Presently `flint` does not use the holography to estimate the apparent brightness of sources. 

Therefore it may be unwise to use such a sky-model as the basis of bandpass calibration. In time this type of prediction may be inforporated into `flint`, but presently it remains as a future to-do. Contributions are welcome. 

## Accessing via the CLI

The primary entry point for the skymodel program in `flint` is the `flint_skymodel`:

```{argparse}
:ref: flint.sky_model.get_parser
:prog: flint_skymodel
```
