# Sky-model calibration

NOTE: Sky-model calibration is still a work in progress and should not be relied upon. It uses an idealised primary beam response (i.e. no holography) and is intended to create a model to bandpass calibrate against.

`flint` provides basic functionality that attempts to create a sky-model to deerive a set
of bandpass solutions against. By using a reference catalogue that describes 2D Gaussian
component positions, their shape and spectral variance, the sky as the telescope sees it
can be estimated. The subsequent model, provided the estimation is correct, can then be
used to run bandpass calibration against.

This functionality was the genesis of the `flint` codebase, but it has not been incorporated
into any of the calibration workflow procedures. It outputs a text file that some tooling
understands. That is to say it does not produce predicted model visibilities that a generic
solver can operate against.

```bash
flint_skymodel -h
# usage: flint_skymodel [-h] [--assumed-alpha ASSUMED_ALPHA] [--assumed-q ASSUMED_Q] [--fwhm-scale FWHM_SCALE] [--flux-cutoff FLUX_CUTOFF] [--cata-dir CATA_DIR]
#                       [--cata-name {NVSS,SUMSS,ICRF,RACSLOW}]
#                       ms

# Create a calibrate compatible sky-model for a given measurement set.

# positional arguments:
#   ms                    Path to the measurement set to create the sky-model for

# options:
#   -h, --help            show this help message and exit
#   --assumed-alpha ASSUMED_ALPHA
#                         Assumed spectral index when no appropriate column in sky-catalogue.
#   --assumed-q ASSUMED_Q
#                         Assumed curvature when no appropriate column in sky-catalogue.
#   --fwhm-scale FWHM_SCALE
#                         Sources within this many FWHMs are selected.
#   --flux-cutoff FLUX_CUTOFF
#                         Apparent flux density (in Jy) cutoff for sources to be above to be included in the model.
#   --cata-dir CATA_DIR   Directory containing known catalogues.
#   --cata-name {NVSS,SUMSS,ICRF,RACSLOW}
#                         Name of catalogue to load. Options are: dict_keys(['NVSS', 'SUMSS', 'ICRF', 'RACSLOW']).
```
