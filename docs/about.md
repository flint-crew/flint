# About

This `flint` package is trying to get a minimum start-to-finish calibration and
imaging workflow. While initially written for `RACS` style ASKAP data a variety of support for larger measurement sets and different imaging modes have been added.

`Flint` is made up of normal `python` functions that perform specialised operations, which are then turned into pipeline stages using `prefect`.

Most of the `python` routines have a CLI that can be used to test them in a
piecewise sense. These entry points are installed as programs available on the
command line. They are listed below with a brief description:

- `flint_skymodel`: derives a sky-model using a reference catalogue suitable to
  perform bandpass calibration against. Note that it is not "science quality" as
  it assumes an ideal primary beam response and the reference catalogues do not
  incorporate spectral information.
- `flint_aocalibrate`: Performs amplitude and phase calibration against a
  sky-model, intended for bandpass calibration, and leverage's Andre Offringa's
  `calibrate` program.
- `flint_flagger`: Performs basic flagging on an input measurement set.
- `flint_bandpass`: A small workflow to bandpass calibrate ASKAP measurement
  sets that have observed PKS B1934-638.
- `flint_ms`: Utility functions related to inspecting and pre-processing an
  ASKAP measurement set.
- `flint_wsclean`: Uses `wsclean` to image and clean an ASKAP measurement set
  with pre-defined options.
- `flint_gaincal`: Uses the `casa` task `gaincal` and `applysolutions` to
  perform self-calibration of an ASKAP measurement set.
- `flint_convol`: Convols a collection of images to a common resolution.
- `flint_yandalinmos`: Will co-add a collection of images of a single field
  together, optionally including holography measurements.
- `flint_config`: The beginnings of a configuration-based scheme to specify
  options throughout a workflow.
- `flint_aegean`: Simple interface to execute BANE and aegean against a provided
  image. These tools are expected to be packaged in a singularity container.
- `flint_validation_plot`: Create a simple, quick look figure that expresses the
  key quality statistics of an image. It is intended to be used against a full
  continuum field image, but in-principal be used for a per beam image.
- `flint_potato`: Attempt to peel out known sources from a measurement set using
  [potatopeel](https://gitlab.com/Sunmish/potato/-/tree/main). Criteria used to
  assess which sources to peel is fairly minimumal, and at the time of writing
  only the reference set of sources packaged within `flint` are
  considered.
-`flint_archive`: Operations around archiving and copying final
  data products into place.
-`flint_catalogue`: Download reference catalogues
  that are expected by `flint`.
- `flint_leakage`: Basic plotting and visualisation of leakage surfaces give Stokes I and V source catalogues amd corresponding images.
- `flint_addmodel`: Predict model visibilities given a BBS style model file using the `addmodel` program.
- `flint_crustalball`: Predict model visibilities given a BBS style model file using the `crystalball` program.

The following commands use the `prefect` framework to link together individual
tasks together (outlined above) into a single data-processing pipeline.

- `flint_flow_bandpass_calibrate`: Executes a prefect flow run that will
  calibrate a set of ASKAP measurement sets taken during a normal bandpass
  observation sequence.
- `flint_flow_continuum_pipeline`: Performs bandpass calibration, solution
  copying, imaging, self-calibration and mosaicing.
- `flint_flow_subtract_cube_pipeline`: Subtract a continuum model and image the
  residual data.
- `flint_flow_polarisation_pipeline`: Perform Stokes I/Q/U imaging of an existing set of calibrated (and self-calibrated) measurement sets.
