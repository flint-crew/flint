# Change log

## Unreleased

- Changes to how `linmos` mosaics are formed, both affecting the flux scale
  - `linmos` builds its output frame from whichever image is listed first, so
    the input images are now ordered with the one nearest the centre of the
    field leading. The centre is taken as the mean of the images' own reference
    directions, so it does not have to be known separately. Beam numbering will
    not do this: `closepack36` numbers beam 0 into a corner of the footprint,
    `square_6x6` does not. Confirmed by running askapsoft's own
    `LinmosAccumulator::setOutputParameters` over a closepack36 footprint: the
    output reference direction comes out exactly equal to that of whichever
    image is listed first, with only the reference pixel moved. Leading with a
    central image also trims the output, 161.8 against 171.6 Mpix for the same
    36 beams, since the bounding box of a centred frame is tighter.

    At the edge of a band 1 field this takes the integrated flux error from
    ~0.93% to ~0.38%. `linmos` has no option for an arbitrary output centre, so
    the most central input image is the best on offer; a frame centred on the
    centroid itself would give ~0.31%, but no beam sits there. Beam 0 is
    already the most central beam of `square_6x6`, so nothing changes for
    those fields.
  - Added `LinmosOptions.regrid_method`, defaulting to `cubic`. The `linmos`
    default of `linear` suppresses the peak of a source by up to ~1.74% at the
    top of band 1 and ~0.77% at 20 arcsec, varying with where the source falls
    between pixels. Measured through casacore's own `ImageRegrid`, the one
    `linmos` drives: `cubic` brings it to ~0.07%, while `lanczos` is worse
    than `cubic` at ~1.1%. Integrated flux is unaffected by the choice.
    `regrid.decimate` was left at its default, having no measurable effect
    between 1 and 10.


# v0.3.0

- Attempts to clean up the prefect tasks related to convolution and linmos
  - Added a `LinmosOptions`
  - Moved some file operations (e.g. deleting files) to the `flint.coadd.linmos`
    module from the prefect tasks
  - added some tests and docstrings
- Changes to how the `--fixed-beam-shape` is used
  - Previously each fits image was convolved before coadding
  - Instead convolve the linmos field image
  - Have not patched this back into the larger flow
- Added `crystalball` for model prediction in `subtract_cube_pipeline`
  - An alternative to the `addmodel` program
  - Patches into the existing Dask client
  - Requires version bump to `numpy>=2.0` in `flint` and associated dependencies
- Removed the `wrapper_get_options_from_strategy`
- Avoid reforming the MS when self-calibrating with CASA
  - using an appropriate `spw=` str and `append=True` in `gaincal`.
- Added `flint_containers` which aims to:
  - Track known containers that are required throughout flint
  - Download (with `singularity-python`) containers that are tracked
  - The beginnings of a `get_known_container` type interface so that containers
    may not need to be referenced via cli all the time
- Improvements around `sky_model`
  - Using `vizier` normalised position units
  - Convert flux columns to Jy
- Improved docs
- Added chunking to the NaN/UVW/zero flagging during selfcal
- Added `update_bane_options` and `update_aegean_options` to `flint.source_finding.aegean`
  - Initially to support using more cores
  - Patched in the `get_options_from_strategy` function in the continuum flow

# 0.2.13

- Added a `flint_flow_polarisation_pipeline`, implemented by @AlecThomson
  - changes to `image_set`
  - `poetry` to `hatchling`
  - using `vcs` version scheme
  - added read the docs integration

# 0.2.9

- Created `subtract_cube_pipeline.py`. Associated changes include:
  - wsclean imaging will delete files while still in temporary storage (for
    instance is a ramdisk is used)
  - added option to prefect convol task to delete files once they have be
    convoled
  - predicting model produced by `wsclean -save-source-list` into measurement
    set
  - using `taql` to subtract model from nominated data column
  - added a 'flow_addmodel_to_mss` to allow different task runner to be
    specified
  - added `flint_no_log_wsclean_output` to `WSCleanOptions` to disable wsclean
    logging
  - added flag to disable logging singularity command output to the
    `flint.logging.logger`
  - subtract flow will remove files whenever possible (remove original files
    after convolving, removing convolved files after linmos, remove channel
    linmos images after combining into a cube, removing the weight text files)
- Added a `timelimit_on_context` helper to raise an error after some specified
  length of time. Looking at you, BANE and issue #186. Arrrr.
- Added a `BANE` callback handler to attempt to help #186. This includes a
  `AttemptRerunException` and corresponding code in `run_singularity_command` to
  retry the failing command.

# 0.2.8

- added `wrapper_options_from_strategy` decorator helper function
- Created `BaseOptions` from a `pydantic.BaseModel` class.
- Added functions to
  - create `ArgumentParser` options from a `BaseOptions` class
  - load arguments into an `BaseOptions` class from a `ArgumentParser`
  - starting to move some classes over to this approaches (including some CLIs)
- Added a verify tarball function to verify archives
- Removed `suppress_artefact` and `minimum_absolute_clip` functions from
  `flint.masking`
- Added an adaptive box selection mode to the minimum absolute algorithm
- Update a MSs `MODEL_DATA` column using `addmodel` and a source list (see
  `wsclean -save-source-list`)
- Added a `taql` based function intended to be used to subtract model data from
  nominated data, `flint.ms.subtract_model_from_data_column`

# 0.2.7

- added in convolving of cubes to common resolution across channels
- cubes are supported when computing the yandasoft linmos weights and trimming
- `--coadd-cubes` option added to co-add cubes on the final imaging round
  together to form a single field spectral cube
- Cleaning up the `flint_masking` CLI:
  - added more options
  - removed references to butterworth filter
  - marked `minimum_boxcar)artefact_mask` as deprecated and to be removed
- Added initial `beam_shape_erode` to masking operations

# 0.2.6

- if `-temp-dir` used in wsclean then imaging products are produced here and
  then copied over to the same directory as the MS. This is intended to make use
  of compute nodes and fast local storage, like memory tmpfs or local disks.
- added stokes-v imaging. This includes a couple of minor / largish changes
  - the `name` attribute of `WSCleanOptions` is not recognised
  - the `pol` attribute of `WSCleanOptions` now needs to be a `str`
  - the `-name` CLI argument of `wsclean` is auto-generated and always provided,
    and will now always contain the `pol` values (i.e. has a `polVALUE` field in
    output filenames)
  - the strategy format now has a `operations` set of keywords, including
    `stokesv` to drawn options from
  - naming format of output linmos files could contain the pol field
  - `stokesv` imaging will not linmos the cleaning residuals together, even if
    the `--linmos-residuals` CLI is provided
- Capture `CleanDivergenceError` from `wsclean` and rerun with larger image size
  and lower gain
- Added `flint.catalogue`, which aims to collect all the catalogue related
  operations
  - a `flint_catalogue` CLI program to:
    - download reference catalogues that are known and expected from vizier
    - verify reference catalogues conform to expectations
    - list the reference catalogues that are expected
- Added `flint.leakage` CLI program to attempt to characterise leakage over
  polarisations, e.g. V/I
- removing the `pol` string in the polarisation field of the
  `ProcessedNameComponents`
  - `wclean` output `-` separation character chhanged to `.`
- The median RMS of the field image is calculated on an inner region when
  cnnstructing the validation plot
- Limit the number of sources to overlay on the RMS plot in the quick look
  validation figure
- Replace the `cassatasks` with a casa in a container
  - Added `flint.sclient.singularity_wrapper` decorator to help make running
    commands in a container easier
  - Removed `casatasks` and `casadata` from dependencies
- Moving to a minimum python=3.12 version
  - Partly enabled by removing `casatasks` and `casadata`, partly required by
    other dependencies that changed
  - Notably the `numpy.distutils` started to complain

# 0.2.5

- added in skip rounds for masking and selfcal
- Basic handling of CASDA measurement sets (preprocessing)
- Basic handling of environment variables in Options, only supported in
  WSCleanOptions (no need for others yet)
- basic renaming of MS and shuffling column names in place of straight up
  copying the MS
- added CLI argument `--fixed-beam-shape` to specify a fixed final resolution,
  overwritng the optimal beam shape that otherwise would be computed
- Added a SlurmInfo class to help with debugging crashed jobs. Primitive and
  likely to change.
- Made the `calibrated_bandpass_path` and optional CLI argument so that CASDA
  MSs can be better handled
- copying folders in `copy_files_into` when archiving
- added reading of ArchiveOptions from strategy file for continuum pipeline and
  `flint_archive`
- Adaptive colour bar scaling in the rms validation plot
- Create multiple linmos images if `--fixed-beam-shape` specified, one at an
  optimal common resolution and another at the specified resolution
- Dump the `FieldOptions` to the output science directory
- Weights produced by `linmos` are also trimmed in the same way as the
  corresponding image

## 0.2.4

- Added new masking modes
- More Option types available in the template configuration
- Tarball of files and linmos fields
- Added context managers to help with MEMDIR / LOCALDIR like variables

## Pre 0.2.3

Everything chsnges all the time
