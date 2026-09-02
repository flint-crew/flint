"""Contains the core of the option class containers that are used to
hold stateful properties throughout the flint codebase.
"""

# NOTE: Although these options could be stored closer to where
# their logic is often used, at times these can cause circular dependencies.
# This happens a lot with the linting / typing checking, where classes are
# imported purely for tools like ruff

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self, TypeAlias

import numpy as np
import yaml
from astropy.coordinates import EarthLocation, SkyCoord
from astropy.time import Time
from capn_crunch import BaseOptions
from pydantic import Field, create_model

from flint.exceptions import MSError
from flint.logging import logger


class BandpassOptions(BaseOptions):
    """Container that represents the flint related options that
    might be used throughout the processing of bandpass calibration
    data.

    In its present form this `BandpassOptions` class is not intended
    to contain properties of the data that arebeing processed, rather
    how these data will be processed.

    These settings are not meant to be adjustabled throughout
    a single bandpass pipeline run
    """

    flagger_container: Path | None = None
    """Path to the singularity aoflagger container"""
    calibrate_container: Path | None = None
    """Path to the singularity calibrate container"""
    expected_ms: int = 36
    """The expected number of measurement set files to find"""
    smooth_solutions: bool = False
    """Will activate the smoothing of the bandpass solutions"""
    smooth_window_size: int = 16
    """The width of the smoothing window used to smooth the bandpass solutions"""
    smooth_polynomial_order: int = 4
    """The polynomial order used by the Savgol filter when smoothing the bandpass solutions"""
    flag_calibrate_rounds: int = 3
    """The number of times the bandpass will be calibrated, flagged, then recalibrated"""
    minuv: float | None = None
    """The minimum baseline length, in meters, for data to be included in bandpass calibration stage"""
    preflagger_ant_mean_tolerance: float = 0.2
    """Tolerance that the mean x/y antenna gain ratio test before the antenna is flagged"""
    preflagger_mesh_ant_flags: bool = False
    """Share channel flags from bandpass solutions between all antenna"""
    preflagger_jones_max_amplitude: float | None = None
    """Flag Jones matrix if any amplitudes with a Jones are above this value"""


class AddModelSubtractFieldOptions(BaseOptions):
    """Options related to predicting a continuum model during the SubtractFieldOptions workflow.
    Specifically these options deal with identifying the wsclean produced source list model, which
    may be used by ``admodel`` to predict model visibilities. See utilities around the ``aocalibrate``
    functions and routines."""

    wsclean_pol_mode: list[str] = ["i"]
    """The polarisation of the wsclean model that was generated"""
    calibrate_container: Path | None = None
    """Path to the container with the calibrate software (including addmodel)"""
    addmodel_cluster_config: Path | None = None
    """Specify a new cluster configuration file different to the preferred on. If None, drawn from preferred cluster config"""


class SubtractFieldOptions(BaseOptions):
    """Container for options related to the
    continuum-subtracted pipeline"""

    wsclean_container: Path
    """Path to the container with wsclean"""
    yandasoft_container: Path
    """Path to the container with yandasoft"""
    subtract_model_data: bool = False
    """Subtract the MODEL_DATA column from the nominated data column"""
    data_column: str = "CORRECTED_DATA"
    """Describe the column that should be imaed and, if requested, have model subtracted from"""
    expected_ms: int = 36
    """The number of measurement sets that should exist"""
    imaging_strategy: Path | None = None
    """Path to a FLINT imaging yaml file that contains settings to use throughout imaging"""
    holofile: Path | None = None
    """Path to the holography FITS cube that will be used when co-adding beams"""
    linmos_residuals: bool = False
    """Linmos the cleaning residuals together into a field image"""
    beam_cutoff: float = 150
    """Cutoff in arcseconds to use when calculating the common beam to convol to"""
    pb_cutoff: float = 0.1
    """Primary beam attenuation cutoff to use during linmos"""
    stagger_delay_seconds: float | None = None
    """The delay, in seconds, that should be used when submitting items in batches (e.g. looping over channels)"""
    attempt_subtract: bool = False
    """Attempt to subtract the model column from the nominated data column"""
    subtract_data_column: str = "DATA"
    """Should the continuum model be subtracted, where to store the output. This will update the column to be imaged."""
    predict_wsclean_model: bool = False
    """Search for the continuum model produced by wsclean and subtract"""
    use_addmodel: bool = False
    """Invoke the ``addmodel`` visibility prediction, including the search for the ``wsclean`` source list"""
    use_crystalball: bool = False
    """Attempt to predict the model visibilities using ``crystalball``"""
    subtract_only: bool = False
    """Only perform the continuum subtraction"""
    timestep_image: bool = False
    """Perform timestep imaging after subtraction"""
    channelwise_image: bool = False
    """Perform channel-wise imaing of the residuals"""
    max_intervals: int = 500
    """The maximum number of scans/channels to consider"""


class FieldOptions(BaseOptions):
    """Container that represents the flint related options that
    might be used throughout components related to the actual
    pipeline.

    In its present form this `FieldOptions` class is not intended
    to contain properties of the data that are being processed,
    rather how those data will be processed.

    These settings are not meant to be adjustable throughout
    rounds of self-calibration.
    """

    flagger_container: Path | None = None
    """Path to the singularity aoflagger container"""
    calibrate_container: Path | None = None
    """Path to the singularity calibrate container"""
    casa_container: Path | None = None
    """Path to the singularity CASA container"""
    expected_ms: int = 36
    """The expected number of measurement set files to find"""
    wsclean_container: Path | None = None
    """Path to the singularity wsclean container"""
    yandasoft_container: Path | None = None
    """Path to the singularity yandasoft container"""
    potato_container: Path | None = None
    """Path to the singularity potato peel container"""
    holofile: Path | None = None
    """Path to the holography FITS cube that will be used when co-adding beams"""
    rounds: int = 2
    """Number of required rouds of self-calibration and imaging to perform"""
    skip_selfcal_on_rounds: list[int] | None = None
    """Do not perform the derive and apply self-calibration solutions on these rounds"""
    zip_ms: bool = False
    """Whether to zip measurement sets once they are no longer required"""
    run_aegean: bool = False
    """Whether to run the aegean source finding tool"""
    aegean_container: Path | None = None
    """Path to the singularity aegean container"""
    no_imaging: bool = False
    """Whether to skip the imaging process (including self-calibration)"""
    reference_catalogue_directory: Path | None = None
    """Path to the directory container the reference catalogues, used to generate validation plots"""
    linmos_residuals: bool = False
    """Linmos the cleaning residuals together into a field image"""
    beam_cutoff: float = 150
    """Cutoff in arcseconds to use when calculating the common beam to convol to"""
    fixed_beam_shape: tuple[float, float, float] | None = None
    """Specify the final beamsize of linmos field images in (arcsec, arcsec, deg)"""
    pb_cutoff: float = 0.1
    """Primary beam attenuation cutoff to use during linmos"""
    use_preflagger: bool = False
    """Whether to apply (or search for solutions with) bandpass solutions that have gone through the preflagging operations"""
    use_smoothed: bool = False
    """Whether to apply (or search for solutions with) a bandpass smoothing operation applied"""
    use_beam_masks: bool = False
    """Construct beam masks from MFS images to use for the next round of imaging. """
    use_beam_masks_from: int = 1
    """If `use_beam_masks` is True, this sets the round where beam masks will be generated from"""
    use_beam_masks_rounds: list[int] | None = None
    """If `use_beam_masks` is True, this sets which rounds should have a mask applied"""
    imaging_strategy: Path | None = None
    """Path to a FLINT imaging yaml file that contains settings to use throughout imaging"""
    sbid_archive_path: Path | None = None
    """Path that SBID archive tarballs will be created under. If None no archive tarballs are created. See ArchiveOptions. """
    sbid_copy_path: Path | None = None
    """Path that final processed products will be copied into. If None no copying of file products is performed. See ArchiveOptions. """
    rename_ms: bool = False
    """Rename MSs throughout rounds of imaging and self-cal instead of creating copies. This will delete data-columns throughout. """
    stokes_v_imaging: bool = False
    """Specifies whether Stokes-V imaging will be carried out after the final round of imagine (whether or not self-calibration is enabled). """
    coadd_cubes: bool = False
    """Co-add cubes formed throughout imaging together. Cubes will be smoothed channel-wise to a common resolution. Only performed on final set of images"""
    update_model_data_with_source_list: bool = False
    """Attempt to update a MSs MODEL_DATA column with a source list (e.g. source list output from wsclean)"""
    use_jolly_tukey_tractor: bool = False
    """Use the jolly roger tukey tractor. See the TukeyTractorOptions and the jolly-roger package for more details."""
    casda_bandpass_table: Path | None = None
    """The bandpass table applied to the MSs for this SBID, as deposited onto CASDA. Used to identify antennas to flag that may be unflagged under certain conditions."""


class PolFieldOptions(BaseOptions):
    """Container that represents the flint related options that
    might be used throughout components related to the actual
    pipeline.

    In its present form this `PolFieldOptions` class is not intended
    to contain properties of the data that are being processed,
    rather how those data will be processed.

    These settings are not meant to be adjustable across different polarisations.
    """

    expected_ms: int = 36
    """The expected number of measurement set files to find"""
    wsclean_container: Path | None = None
    """Path to the singularity wsclean container"""
    yandasoft_container: Path | None = None
    """Path to the singularity yandasoft container"""
    casa_container: Path | None = None
    """Path to the singularity CASA container"""
    holofile: Path | None = None
    """Path to the holography FITS cube that will be used when co-adding beams"""
    beam_cutoff: float = 150
    """Cutoff in arcseconds to use when calculating the common beam to convol to"""
    fixed_beam_shape: tuple[float, float, float] | None = None
    """Specify the final beamsize of linmos field images in (arcsec, arcsec, deg)"""
    pb_cutoff: float = 0.1
    """Primary beam attenuation cutoff to use during linmos"""
    bane_noise: FFTBANEOptions | None = None
    """Opt in to measuring a background and RMS cube off the co-added planes with BANE (see ``flint.bane``). These describe the natural-resolution cubes this stage archives. None, the default, skips it. Independent of ``RMSynthFieldOptions.bane_noise``, which is the one the FDF noise comes from"""
    imaging_strategy: Path | None = None
    """Path to a FLINT imaging yaml file that contains settings to use throughout imaging"""
    pol_cube_channel_width: float | None = None
    """Desired width, in Hz, of each plane of the polarisation cubes. The wsclean channel division is solved for this target so the cubes have a single linear frequency axis, overriding the strategy ``channels_out``. Deliberately separate from ``RACSAllOptions.cube_channel_width``, as the continuum and polarisation cubes need not share a channelisation. See ``flint.imager.channel_division``"""
    sbid_copy_path: Path | None = None
    """Path that final processed products will be copied into. If None no copying of file products is performed. See ArchiveOptions. """


class FFTBANEOptions(BaseOptions):
    """Options for ``flint.bane.robust_bane``. Named apart from
    ``flint.source_finding.aegean.BANEOptions``, which drives the containerised
    aegean BANE instead.

    Defined here rather than in ``flint.bane`` so that strategy validation
    (``flint.configuration``) does not pull in numba, as ``RMSynthOptions`` does
    for rm_lite."""

    step_size: int | None = None
    """Downsampling factor in pixels. None uses 3 beams; a negative value sets the beams per step"""
    box_size: int | None = None
    """Convolution kernel size in pixels. None uses 10 beams; a negative value sets the beams per box"""
    clip_sigma: float = 5.0
    """Pixels above this SNR are replaced by noise before the background is fitted"""
    seed: int = 1234
    """Seed for the noise the clipped pixels are filled with, so a rerun reproduces the maps"""


class _StokesTrio(BaseOptions):
    """One path per Stokes, shared by the containers below.

    Never annotate with this directly: the whole point of the three concrete
    types is that a cube cannot be mistaken for a noise or a weight, and a base
    they all satisfy would let exactly that through.
    """

    q: Path
    """Stokes Q"""
    u: Path
    """Stokes U"""
    i: Path | None = None
    """Stokes I. Optional throughout: rm-synthesis runs without the fractional-polarisation correction"""

    @classmethod
    def from_mapping(cls, cubes: Mapping[str, Path]) -> Self:
        """From a per-Stokes dict, as the polarisation stage keys them.

        Anything but q/u/i is dropped, and a missing q or u is refused here
        rather than surfacing as a KeyError from the call site.
        """
        missing = {"q", "u"} - cubes.keys()
        if missing:
            msg = f"Need a cube for every one of q and u, missing {sorted(missing)}."
            raise ValueError(msg)
        return cls(q=cubes["q"], u=cubes["u"], i=cubes.get("i"))

    @property
    def paths(self) -> list[Path]:
        """Every path set, for callers that only validate or clean them up"""
        return [path for path in (self.q, self.u, self.i) if path is not None]


class StokesCubes(_StokesTrio):
    """The Stokes image cubes rm-synthesis reads"""


class StokesWeightCubes(_StokesTrio):
    """Per-pixel inverse variance, 1/sigma**2, as linmos writes it"""

    kind: Literal["weight"] = "weight"
    """Discriminates the union below. Never set it by hand"""


class StokesNoiseCubes(_StokesTrio):
    """Per-pixel noise, sigma, as BANE measures it"""

    kind: Literal["noise"] = "noise"
    """Discriminates the union below. Never set it by hand"""


StokesErrorCubes: TypeAlias = Annotated[
    StokesWeightCubes | StokesNoiseCubes, Field(discriminator="kind")
]
"""The error cubes rm-synthesis weights by: an inverse variance or a noise, never
both and never something ambiguous. rm-lite takes either through one argument and
is told which by a flag, so mixing them inverts the noise by 1/sigma**2. The
discriminator is what keeps that straight across a serialisation round trip, which
prefect does to every task input."""


class RMSynthOptions(BaseOptions):
    """Options controlling ``rm_lite.tools_3d.rmsynth.rmsynth_3d_from_fits``.

    Defined here rather than in ``flint.rmsynth`` so that strategy validation
    (``flint.configuration``) does not need to import ``rm_lite``, an optional
    dependency.
    """

    phi_max_radm2: float | None = None
    """Maximum Faraday depth to synthesise, in rad/m^2"""
    d_phi_radm2: float | None = None
    """Faraday depth resolution, in rad/m^2"""
    n_samples: float | None = 10.0
    """Number of samples across the RMSF"""
    weight_type: Literal["variance", "natural", "uniform", "uniform_lsq", "briggs"] = (
        "variance"
    )
    """Per-channel weighting scheme used during RM-synthesis"""
    robust: float | None = None
    """Briggs robust parameter, required if weight_type is 'briggs'"""
    nufft_nthreads: int = 1
    """finufft OpenMP threads per dask chunk"""
    target_chunk_mb: float = 256
    """Target per-chunk memory footprint, in MB, when reading the Q/U cubes"""
    fit_order: int = 2
    """Stokes I fractional-polarisation fit order; negative iterates orders and picks the best by AIC"""
    fit_function: Literal["log", "linear"] = "log"
    """Stokes I fit function: 'log' is a power law, 'linear' is a polynomial"""
    stokes_i_snr_cut: float | None = 5.0
    """Below this frequency-averaged Stokes I SNR a pixel falls back to a flat model. None fits every pixel"""
    compute_model_error: bool = False
    """Monte-Carlo the Stokes I fit's per-pixel model error via n_error_samples resamples. Only used if a Stokes I cube is given"""
    n_error_samples: int = 1000
    """Monte-Carlo resamples used by compute_model_error"""
    debias_moments: bool = False
    """Also compute a debiased (via rm_lite's debias_fdf) mom0/mom1/mom2 set per requested FDF"""
    debias_filter_size: int = 5
    """Median filter size (pixels) used by mom0 debiasing"""
    lam_sq_0_m2: float | Literal["auto", "per_pixel"] = "auto"
    """Reference lambda^2 the FDF is derotated to. 'auto' picks one value for the whole cube; 'per_pixel' gives each pixel its own, which also forces per_pixel_rmsf since the RMSF then differs pixel to pixel. A float pins it explicitly"""
    per_pixel_rmsf: bool = False
    """Compute the RMSF for each pixel rather than one shared by the cube. Roughly doubles the FDF's size. rm-lite turns this on itself when the weights make pixels disagree, as the linmos weight cubes do"""
    estimate_stokes_i_noise: bool = True
    """Derive the per-channel Stokes I error from the Stokes I cube when no Stokes I weight cube is given. A weight cube takes precedence"""


class RMCleanOptions(BaseOptions):
    """Options controlling ``rm_lite.tools_3d.rmclean.run_rmclean_from_synth``.
    See ``RMSynthOptions`` for why this lives here rather than ``flint.rmsynth``.
    """

    auto_mask: float = 7
    """Masking threshold in SNR, scaled by the theoretical FDF noise"""
    auto_threshold: float = 1
    """Cleaning threshold in SNR, scaled by the theoretical FDF noise"""
    max_iter: int = 100_000
    """Maximum CLEAN iterations"""
    gain: float = 0.1
    """CLEAN loop gain"""
    moment_threshold_snr: float = 5.0
    """SNR cut (times the theoretical FDF noise) applied before computing Faraday moment maps, the dirty ones included"""
    peak_threshold_snr: float = 0.0
    """SNR cut (times the theoretical FDF noise) below which FDF peak statistics are blanked. Zero applies no cut: unlike mom0, a peak is a single sample with no noise floor to integrate, and peak_pi_error is written beside it to judge significance downstream"""


class SpiceOptions(BaseOptions):
    """Options controlling the SPICE-style cube trimming (see ``flint.spice``)

    Mask everything outside small boxes around catalogued sources, crop to
    their union, and compress. Column names/units for a user-supplied
    ``SpiceFieldOptions.catalogue`` are specified via the ``catalogue_*`` fields
    """

    n_beamwidths: float = 3.0
    """Padding added to each side of an island's bounding box, in units of the restoring beam major axis"""
    catalogue_island_col: str | None = None
    """Column grouping components into islands in a user-supplied catalogue. None treats each row as its own island"""
    catalogue_ra_col: str | None = None
    """RA column in a user-supplied catalogue. Required whenever a catalogue is supplied"""
    catalogue_dec_col: str | None = None
    """Dec column in a user-supplied catalogue. Required whenever a catalogue is supplied"""
    catalogue_radec_unit: str = "deg"
    """Astropy unit string for catalogue_ra_col/catalogue_dec_col"""
    catalogue_maj_col: str | None = None
    """Major-axis column in a user-supplied catalogue. None disables ellipse sizing (point-source + beamwidth padding only)"""
    catalogue_min_col: str | None = None
    """Minor-axis column in a user-supplied catalogue"""
    catalogue_pa_col: str | None = None
    """Position-angle column in a user-supplied catalogue"""
    catalogue_shape_unit: str = "arcsec"
    """Astropy unit string for catalogue_maj_col/catalogue_min_col. catalogue_pa_col is always degrees"""
    catalogue_sizes_deconvolved: bool | None = None
    """Whether catalogue_maj_col/catalogue_min_col are PSF-deconvolved rather than as-observed. Required whenever catalogue_maj_col is set"""
    catalogue_psf_maj_col: str | None = None
    """Per-source PSF major-axis column, used to re-convolve when catalogue_sizes_deconvolved is True. Unset falls back to the pipeline's common restoring beam"""
    catalogue_psf_min_col: str | None = None
    """Column name for the per-source PSF minor axis, paired with catalogue_psf_maj_col"""
    catalogue_psf_pa_col: str | None = None
    """Column name for the per-source PSF position angle, paired with catalogue_psf_maj_col"""
    compress_method: Literal["gzip", "pgzip"] = "pgzip"
    """Compression backend for the mandatory gzip of every spiced cube."""
    compress_max_workers: int | None = None
    """Thread count handed to the compression backend"""


class RMSynthFieldOptions(BaseOptions):
    """Options for running RM-synthesis (and optionally RM-CLEAN) as its own
    standalone pipeline, given already-imaged Stokes Q/U cubes. See
    ``RMSynthOptions``/``RMCleanOptions`` for the RM-synthesis/RM-CLEAN
    algorithm parameters, which are drawn from ``imaging_strategy`` rather
    than exposed here."""

    stokes_cubes: StokesCubes | None = None
    """The Stokes Q/U (and optionally I) FITS cubes. Computed by the racs-all flow, so required only when running this pipeline standalone"""
    error_cubes: StokesErrorCubes | None = None
    """The linmos weight cubes or a set of noise cubes, never both: see ``StokesErrorCubes``. None leaves rm-lite to estimate a per-channel noise from Q/U itself. Superseded by ``bane_noise``, which measures the noise at the resolution the FDF is built at"""
    bane_noise: FFTBANEOptions | None = None
    """Opt in to measuring BANE background and RMS cubes off the common-resolution cubes this stage writes, and using the RMS for the FDF noise (see ``flint.bane``). Measured after the convolution, so they describe the resolution the FDF is built at and supersede ``error_cubes``, which describe the unconvolved inputs. None, the default, falls back to whatever cubes the caller passed"""
    imaging_strategy: Path | None = None
    """Path to a FLINT imaging yaml file that contains the RMSynthOptions/RMCleanOptions settings to use"""
    beam_cutoff: float | None = None
    """Cutoff in arcseconds to use when bringing the input cubes to a common beam. Channels coarser than this are blanked instead of dragging every channel out to their resolution. Defaults to no cutoff"""
    cube_products: list[Literal["dirty", "clean", "model"]] = []
    """Which Faraday dispersion function (FDF) cubes to write as FITS. Nothing by default, as these cubes can be large."""
    moment_products: list[Literal["dirty", "clean", "model"]] = ["clean"]
    """Which FDF(s) to compute Faraday moment maps from."""
    peak_products: list[Literal["dirty", "clean", "model"]] = []
    """Which FDF(s) to measure peak statistics from: peak polarised intensity (raw and debiased), Faraday depth, polarisation angle and intrinsic angle, each with its error. Nine (ny, nx) maps per FDF, so empty by default -- at 16032^2 that is ~9 GB per entry"""
    output_path: Path | None = None
    """Directory the FDF cube and moment products are written into. Defaults to alongside the input Stokes cubes"""
    sbid_copy_path: Path | None = None
    """Path that final processed products will be copied into. If None no copying of file products is performed. See ArchiveOptions. """


class SpiceFieldOptions(BaseOptions):
    """Options for running SPICE-style cube compression as its own standalone
    pipeline, given already-imaged Stokes cubes. See ``SpiceOptions`` for the
    trimming/compression algorithm parameters, which are drawn from
    ``imaging_strategy`` rather than exposed here."""

    cubes: list[Path] = []
    """Stokes image cubes to trim and compress. Computed by the racs-all flow, so required only when running this pipeline standalone"""
    weight_cubes: list[Path] = []
    """Ancillary cubes to trim and compress alongside ``cubes``, spiced with the same boxes so they stay on a matching grid: the LINMOS weight cubes, and the BANE background/RMS cubes when the polarisation stage made them. Computed by the racs-all flow"""
    reference_image: Path | None = None
    """A 2D MFS image whose WCS/shape sources the source-finding boxes. Required only when catalogue is not set (built-in aegean source finding)"""
    catalogue: Path | None = None
    """A source catalogue (RA/Dec at minimum). If None the pipeline source finds its own sources from reference_image instead. See SpiceOptions for how to describe this catalogue's columns"""
    aegean_container: Path | None = None
    """Path to the singularity aegean container. Required when catalogue is not set (built-in source finding)"""
    imaging_strategy: Path | None = None
    """Path to a FLINT imaging yaml file that contains the SpiceOptions settings to use"""
    output_path: Path | None = None
    """Directory the spiced cubes are written into, replacing the originals. Defaults to leaving each cube in place"""
    sbid_copy_path: Path | None = None
    """Path that final processed products will be copied into. If None no copying of file products is performed. See ArchiveOptions. """


class RACSAllOptions(BaseOptions):
    """Options to use throughout the RACS-All processing workflow. Based
    on the continuum self-calibration flow. In the current form this will
    be processing data from CASDA, i.e. no bandpass applied.

    In its present form this `FieldOptions` class is not intended
    to contain properties of the data that are being processed,
    rather how those data will be processed.
    """

    low_data: Path
    """Path to the low data to process"""
    mid_data: Path
    """Path to the mid data to process"""
    high_data: Path
    """Path to the high data to process"""
    flagger_container: Path | None = None
    """Path to the singularity aoflagger container"""
    casa_container: Path | None = None
    """Path to the singularity CASA container"""
    expected_ms: int = 36
    """The expected number of measurement set files to find"""
    wsclean_container: Path | None = None
    """Path to the singularity wsclean container"""
    yandasoft_container: Path | None = None
    """Path to the singularity yandasoft container"""
    potato_container: Path | None = None
    """Path to the singularity potato peel container"""
    low_holofile: Path | None = None
    """Path to the holography FITS cube for the low-band data that will be used when co-adding beams"""
    mid_holofile: Path | None = None
    """Path to the holography FITS cube for the mid-band data that will be used when co-adding beams"""
    high_holofile: Path | None = None
    """Path to the holography FITS cube for the high-band data that will be used when co-adding beams"""
    rounds: int = 2
    """Number of required rouds of self-calibration and imaging to perform"""
    zip_ms: bool = False
    """Whether to zip measurement sets once they are no longer required"""
    run_aegean: bool = False
    """Whether to run the aegean source finding tool"""
    aegean_container: Path | None = None
    """Path to the singularity aegean container"""
    reference_catalogue_directory: Path | None = None
    """Path to the directory container the reference catalogues, used to generate validation plots"""
    linmos_residuals: bool = False
    """Linmos the cleaning residuals together into a field image"""
    beam_cutoff: float = 150
    """Cutoff in arcseconds to use when calculating the common beam to convol to"""
    pb_cutoff: float = 0.1
    """Primary beam attenuation cutoff to use during linmos"""
    use_beam_masks: bool = False
    """Construct beam masks from MFS images to use for the next round of imaging. """
    imaging_strategy: Path | None = None
    """Path to a FLINT imaging yaml file that contains settings to use throughout imaging"""
    sbid_archive_path: Path | None = None
    """Path that SBID archive tarballs will be created under. If None no archive tarballs are created. See ArchiveOptions. """
    sbid_copy_path: Path | None = None
    """Path that final processed products will be copied into. If None no copying of file products is performed. See ArchiveOptions. """
    rename_ms: bool = False
    """Rename MSs throughout rounds of imaging and self-cal instead of creating copies. This will delete data-columns throughout. """
    coadd_cubes: bool = False
    """Co-add cubes formed throughout imaging together. Cubes will be smoothed channel-wise to a common resolution. Only performed on final set of images"""
    cube_channel_width: float | None = None
    """Desired width, in Hz, of each plane of the final cube. The wsclean channel division is solved for this target so the cube has a single linear frequency axis, overriding the strategy ``channels_out`` in the final round. See ``flint.imager.channel_division``"""
    holofile: Path | None = None
    """The oath to a concatenated holography FITS file that contains low-, mid- and high-band cubes"""


def pol_field_options_cli_class(
    racs_all_options_class: type[RACSAllOptions] = RACSAllOptions,
) -> type[PolFieldOptions]:
    """Build a ``PolFieldOptions`` subclass containing only the fields not already
    present on ``RACSAllOptions``, so it can be added to the same CLI parser without
    duplicate flags for the fields the two share. Every current and future
    ``PolFieldOptions`` field is exposed on the CLI this way, either through this
    class or through ``RACSAllOptions`` itself for the shared ones."""
    overlap = set(racs_all_options_class.model_fields) & set(
        PolFieldOptions.model_fields
    )
    unique_fields = {
        name: (field.annotation, field)
        for name, field in PolFieldOptions.model_fields.items()
        if name not in overlap
    }
    return create_model(
        "PolFieldOptionsCLI", __base__=PolFieldOptions.__base__, **unique_fields
    )


class RACSAllPipelineOptions(BaseOptions):
    """Options controlling the ``racs-all`` flow-of-flows.

    Execution order: imaging -> polarisation -> rm-synth/clean -> spice-compression

    with each stage individually skippable.
    """

    output_path: Path | None = None
    """Root directory the rm-synth and spice stages write their products under, in per-stage subdirectories. Defaults to the continuum stage's science path"""
    skip_imaging: bool = False
    """Skip the continuum imaging/self-calibration stage"""
    skip_polarisation: bool = False
    """Skip the polarisation imaging stage"""
    skip_rmsynth: bool = False
    """Skip the RM-synthesis/RM-CLEAN stage"""
    skip_spice: bool = False
    """Skip the SPICE compression stage"""
    imaging_cluster_config: Path | None = None
    """Specify a new cluster configuration file for the imaging stage, different to the preferred one. If None, drawn from the preferred cluster config"""
    polarisation_cluster_config: Path | None = None
    """Specify a new cluster configuration file for the polarisation stage, different to the preferred one. If None, drawn from the preferred cluster config"""
    rmsynth_cluster_config: Path | None = None
    """Specify a new cluster configuration file for the rm-synth/clean stage, different to the preferred one. If None, drawn from the preferred cluster config"""
    spice_cluster_config: Path | None = None
    """Specify a new cluster configuration file for the spice-compression stage, different to the preferred one. If None, drawn from the preferred cluster config"""


def dump_field_options_to_yaml(
    output_path: Path,
    field_options: FieldOptions
    | PolFieldOptions
    | SubtractFieldOptions
    | RACSAllOptions,
    overwrite: bool = False,
) -> Path:
    """Dump the supplied instance of `FieldOptions` to a yaml file
    for record keeping.

    The parent directory of the `output_path` will be created if it
    does not already exist.

    Args:
        output_path (Path): Path of the output file.
        field_options (FieldOptions | PolFieldOptions | SubtractFieldOptions | RACSAllOptions): The `FieldOptions` class to write.
        overwrite (bool, optional): Overwrite the file if it exists. Defaults to False.

    Raises:
        FileExistsError: Raise if `output_path` already exists and `overwrite` is `False`

    Returns:
        Path: Output path written to.
    """

    logger.info(f"Writing field_options to {output_path}")

    if not overwrite and output_path.exists():
        raise FileExistsError(f"{output_path=} exists. ")

    # Create the directory just in case
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w") as out_file:
        yaml.dump(data=field_options._asdict(), stream=out_file, sort_keys=False)

    return output_path


# TODO: Perhaps move these to flint.naming, and can be built up
# based on rules, e.g. imager used, source finder etc.
DEFAULT_TAR_RE_PATTERNS = (
    r".*MFS.*image\.fits",
    r".*linmos.*",
    r".*weight\.fits",
    r".*yaml",
    r".*\.txt",
    r".*png",
    r".*beam[0-9]+\.ms\.zip",
    r".*beam[0-9]+\.ms",
    r".*\.caltable",
    r".*\.tar",
    r".*\.csv",
)
DEFAULT_COPY_RE_PATTERNS = (r".*linmos.*fits", r".*weight\.fits", r".*png", r".*csv")


class ArchiveOptions(BaseOptions):
    """Container for options related to archiving products from flint workflows"""

    tar_file_re_patterns: tuple[str, ...] = DEFAULT_TAR_RE_PATTERNS
    """Regular-expressions to use to collect files that should be tarballed"""
    copy_file_re_patterns: tuple[str, ...] = DEFAULT_COPY_RE_PATTERNS
    """Regular-expressions used to identify files to copy into a final location (not tarred)"""


class FitsCubeOptions(BaseOptions):
    """Container of opptions used to combine images into a single cube using the `fitscube` package.
    This is particularly useful to manage the larger concatenations."""

    bounding_box: bool = True
    """Whether to attempt to trim images when combining"""
    max_workers: int = 4
    """The number of concurrent workers (readers/writers) that are permitted at a time"""
    invalidate_zeros: bool = True
    """Set pixels whose values are exactly 0.0 to not-a-number (nan)"""
    compress: bool = False
    """Gzip-compress the output cube once written"""
    compress_method: Literal["gzip", "pgzip"] = "pgzip"
    """The compression backend to use when ``compress`` is set"""
    remove_original_images: bool = True
    """Remove the images that go into forming the fitscube"""
    inplace: bool = True
    """If True, modify the file in-place. If False, write to a temporary file and then replace the original. Default True"""
    create_blanks: bool = True
    """Have fitscube re-grid the input frequencies onto a tolerant regular grid before writing."""


class MSSummary(BaseOptions):
    """Small structure to contain overview of a MS"""

    unflagged: int
    """Number of unflagged records"""
    flagged: int
    """Number of flagged records"""
    flag_spectrum: np.ndarray
    """Flagged spectral channels"""
    fields: list[str]
    """Collection of unique field names from the FIELDS table"""
    ants: list[int]
    """Collection of unique antennas"""
    beam: int
    """The ASKAP beam number of the measurement set"""
    path: Path
    """Path to the measurement set that is being represented"""
    phase_dir: SkyCoord
    """The phase direction of the measurement set, which will be where the image will be centred"""
    spw: int | None = None
    """Intended to be used with ASKAP high-frequency resolution modes, where the MS is divided into SPWs"""
    ms: MS | None = None
    """The MS object used to generate the summary"""
    pol_axis: float | None = None
    """The rotation of the third-axis mount recorded in the MS"""
    location: EarthLocation | None = None
    """Location of the instrument"""
    ms_times: Time | None = None
    """Unique timesteps in the measurement set"""
    integration: float | None = None
    """Length of the observing time in seconds"""


class HasMS(Protocol):
    ms: MS


class MS(BaseOptions):
    path: Path
    """Path to the MS that this instanceis tracking"""
    column: str | None = None
    """If set indicates column that is activate and should be used during imaging or calibration operations"""
    beam: int | None = None
    """If set indicates seam number of the MS"""
    spw: int | None = None
    """If set indicates the SPW that should be used in operations"""
    field: str | None = None
    """If set indicates the field of the data in the MS"""
    model_column: str | None = None
    """If set indicates the column with model visibilities"""

    @property
    def ms(self) -> MS:
        return self

    @classmethod
    def cast(cls, ms: MS | Path | HasMS) -> MS:

        if isinstance(ms, MS):
            pass
        elif isinstance(ms, Path):
            ms = MS(path=ms)
        elif not isinstance(ms, MS) and "ms" in dir(ms) and isinstance(ms.ms, MS):
            ms = ms.ms
        else:
            # Helpful checks that helped figure out issues involving NamedTuples
            logger.debug(f"{not isinstance(ms, (MS, tuple))=}")
            logger.debug(f"{'ms' in dir(ms)=}")

            raise MSError(f"Unable to convert {ms=} of {type(ms)} to MS object. ")

        return ms


def standardise_ms_to_list_ms(
    ms: Path | MS | tuple[MS | Path, ...] | list[MS | Path] | HasMS,
) -> list[MS]:
    """A utility to process a collection of inputs that could be linked to a
    set of MS instances, and output a single list of MS objects. The list may
    be of length 1.

    Useful for when a single task could potentially operate against a set of
    input MSs simuletanously, such as wsclean.

    Args:
        ms (Path | MS | tuple[MS  |  Path, ...] | list[MS  |  Path]): Descriptions to create MS instances

    Raises:
        ValueError: Raised when an empty list is formed

    Returns:
        list[MS]: Set of output MS objects
    """
    output_list: list[MS] = []
    if isinstance(ms, Path):
        output_list.append(MS(path=ms))
    elif isinstance(ms, MS):
        output_list.append(ms)
    elif isinstance(ms, (list, tuple)):
        for item in ms:
            output_list.append(MS.cast(item))
    assert isinstance(output_list, list), f"{type(output_list)=} is not list"

    if not len(output_list) > 0:
        msg = f"Constructed output list of MSs is empty, {output_list=}"
        raise ValueError(msg)

    return output_list
