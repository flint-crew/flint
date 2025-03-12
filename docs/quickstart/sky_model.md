# Sky-model calibration

```{admonition} Caution
:class: caution

Sky-model calibration is still a work in progress and should not be relied upon. It uses an idealised primary beam response (i.e. no holography) when predicting the apparent brightness of sources towards a direction. This level of of precision may not be suited for some purposes (e.g. bandpass calibration).
```

`flint` provides basic functionality that attempts to create a sky-model to deerive a set
of bandpass solutions against. By using a reference catalogue that describes the positions of a set of 2D Gaussian
components, their shape and spectral variance, the sky as the telescope sees it
can be predicted. The subsequent model, provided the estimation is correct, can then be
used to run bandpass calibration against.

This functionality was the genesis of the `flint` codebase, but it has not been incorporated
into any of the calibration workflow procedures. It outputs a text file based on the name of the input measurement set. That is to say it does not produce predicted model visibilities that a generic
solver can operate against.

## Accessing via the CLI

The primary entry point for the skymodel program in `flint` is the `flint_skymodel`:

```{argparse}
:ref: flint.sky_model.get_parser
:prog: flint_skymodel
```
