#!/usr/bin/env python
from __future__ import annotations

from argparse import ArgumentParser
from functools import partial
from pathlib import Path
from typing import NamedTuple

import numpy as np
import yaml
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.table import QTable, Table
from astropy.table.row import Row
from capn_crunch import BaseOptions, add_options_to_parser, create_options_from_parser
from scipy.optimize import curve_fit

from flint.catalogue import KNOWN_REFERENCE_CATALOGUES, Catalogue
from flint.coadd.linmos import compute_pb_rotation_alpha
from flint.logging import logger
from flint.misc.holo import sample_beam_attenuation
from flint.ms import get_freqs_from_ms, get_phase_dir_from_ms, get_pol_axis_as_rad
from flint.naming import extract_beam_from_name
from flint.utils import get_packaged_resource_path

KNOWN_PB_TYPES = ("gaussian", "sincsquared", "airy")


class SkyModelOptions(BaseOptions):
    """Options that describe how to build a local sky-model, including
    where reference catalogues are stored, the preferred catalogue, the
    types of models to produce, and filtering criteria"""

    reference_catalogue_directory: Path = Path(".")
    """The reference catalogue directory that contains the known flint reference catalogues"""
    reference_name: str | None = None
    f"""Name of the preferred reference survey to use (not the filename). See the list of registered known catalogues: {KNOWN_REFERENCE_CATALOGUES.keys()}. """
    assumed_alpha: float = -0.83
    """Assume this to be the typical spectral index if it is not recorded in the reference catalogue"""
    assumed_beta: float = 0.0
    """Assume this to be the typical amount of spectral curvature should they not be in the reference catalogue"""
    flux_cutoff: float = 0.02
    """The intrinsic brightness a source needs to be for it to be included in the sky model"""
    fwhm_scale_cutoff: float = 1
    """A source needs to be within this many FWHM units from the direction of interest for it to be included"""
    write_hyperdrive_model: bool = False
    """Should the model for hyperdrive be created. The output will have .hypderdrive.yaml suffix appended to the MS path."""
    write_calibrate_model: bool = False
    """Should the model for calibrate be created. The output will have .calibrate.txt suffix appended to the MS path."""
    write_ds9_region: bool = False
    """Should a DS9 region file be created. The output will have .ds9.reg suffix appended to the MS path."""
    catalogue_path: Path | None = None
    """Path to a user-supplied catalogue file. If set, takes precedence over reference_catalogue_directory/reference_name. Column names/units below are never guessed."""
    catalogue_freq: float | None = None
    """Reference frequency, in Hz, of catalogue_path's flux column, used for every source. Ignored if catalogue_freq_col is set. One of catalogue_freq/catalogue_freq_col is required whenever catalogue_path is set."""
    catalogue_freq_col: str | None = None
    """Per-source reference-frequency column in catalogue_path. If set, overrides catalogue_freq with a per-row value instead of one value for the whole catalogue."""
    catalogue_freq_unit: str = "Hz"
    """Astropy unit string for catalogue_freq_col"""
    catalogue_ra_col: str | None = None
    """RA column in catalogue_path. Required whenever catalogue_path is set -- never guessed"""
    catalogue_dec_col: str | None = None
    """Dec column in catalogue_path. Required whenever catalogue_path is set"""
    catalogue_flux_col: str | None = None
    """Flux column in catalogue_path. Required whenever catalogue_path is set"""
    catalogue_name_col: str | None = None
    """Source name column in catalogue_path. If unset, names are synthesised (src0, src1, ...)"""
    catalogue_maj_col: str | None = None
    """Major-axis column in catalogue_path. None disables shape info (point sources). Must be set together with catalogue_min_col/catalogue_pa_col"""
    catalogue_min_col: str | None = None
    """Minor-axis column in catalogue_path, paired with catalogue_maj_col"""
    catalogue_pa_col: str | None = None
    """Position-angle column in catalogue_path, paired with catalogue_maj_col. Always degrees"""
    catalogue_sizes_deconvolved: bool | None = None
    """Whether catalogue_maj_col/catalogue_min_col are already intrinsic (PSF-deconvolved) sizes, ready to use directly in the sky model, or as-observed (PSF-convolved) sizes that must be deconvolved first. Required whenever catalogue_maj_col is set -- never guessed"""
    catalogue_psf_maj_col: str | None = None
    """Per-source PSF major-axis column in catalogue_path, used to deconvolve catalogue_maj_col/catalogue_min_col when catalogue_sizes_deconvolved is False. Required in that case -- there is no pipeline restoring beam to fall back on here"""
    catalogue_psf_min_col: str | None = None
    """Per-source PSF minor-axis column, paired with catalogue_psf_maj_col"""
    catalogue_psf_pa_col: str | None = None
    """Per-source PSF position-angle column, paired with catalogue_psf_maj_col. Always degrees"""
    catalogue_alpha_col: str | None = None
    """Spectral index column in catalogue_path. If unset, falls back to assumed_alpha"""
    catalogue_beta_col: str | None = None
    """Spectral curvature column in catalogue_path. If unset, falls back to assumed_beta"""
    catalogue_radec_unit: str = "deg"
    """Astropy unit string for catalogue_ra_col/catalogue_dec_col. Always applied explicitly, never inferred from the file"""
    catalogue_flux_unit: str = "Jy"
    """Astropy unit string for catalogue_flux_col"""
    catalogue_shape_unit: str = "arcsec"
    """Astropy unit string for catalogue_maj_col/catalogue_min_col/catalogue_psf_maj_col/catalogue_psf_min_col"""


class CurvedPL(NamedTuple):
    """Container for results of a Curved Power Law,

    >>> S_nu = S_nu_0 * (nu/nu_0)**alpha * exp(beta*ln(nu/nu_0)**2.)

    Note that in the case of beta=0. the model reduces to a normal power-law.

    """

    # TODO: Should these be quantities?
    norm: float
    """The fitted normalisation of the fitted model"""
    alpha: float
    """The fitted spectral index"""
    beta: float
    """The fitted curvature of the spectral index"""
    ref_nu: float
    """The nominated reference frequency"""


class GaussianResponse(NamedTuple):
    """Container describing a simple Gaussian taper"""

    freqs: np.ndarray
    """The frequencies the beam is evaluated at"""
    atten: np.ndarray
    """The attenuation of the response"""
    fwhms: np.ndarray
    """The full-width at half-maximum corresponding to freqs"""
    offset: float
    """Angular offset of the source"""


class SincSquaredResponse(NamedTuple):
    """Container describing a sinc-squared response"""

    freqs: np.ndarray
    """The frequencies the beam is evaluated at"""
    atten: np.ndarray
    """The attenuation of the response"""
    fwhms: np.ndarray
    """The full-width at half-maximum corresponding to freqs"""
    offset: float
    """Angular offset of the source"""


class AiryResponse(NamedTuple):
    """Container describing a airy disc response"""

    freqs: np.ndarray
    """The frequencies the beam is evaluated at"""
    atten: np.ndarray
    """The attenuation of the response"""
    fwhms: np.ndarray
    """The full-width at half-maximum corresponding to freqs"""
    offset: float
    """Angular offset of the source"""


class SkyModel(NamedTuple):
    """Description of the derived sky-model"""

    flux_jy: float
    """Total flux in Jansky"""
    no_sources: int
    """Number of source that are included in the sky-model"""
    apparent: bool = True
    """Whether the sources and model are absolute of apparent fluxes"""
    hyperdrive_model: Path | None = None
    """Path to the sky-model file created to use with hyperdrive"""
    calibrate_model: Path | None = None
    """Path to the sky-model file created to use with calibrate"""
    ds9_region: Path | None = None
    """Path to the DS9 region file representing the sky-model"""


# These columns are what we will normalise the all columns and units to
NORM_COLS = {"flux": "Jy", "maj": "arcsecond", "min": "arcsecond", "pa": "deg"}
"""Normalised column names and their corresponding astropy units. """

KNOWN_CATAS: dict[str, Catalogue] = KNOWN_REFERENCE_CATALOGUES
"""Known sky-model catalogues that have had some pre-processing operations applied. Discuss with maintainers for access, """

# TODO: Make this a yaml file packaged in data/models
KNOWN_1934_FILES = {"calibrate": "1934-638.calibrate.txt"}
"""Known models of PKS B1934-638 in different formats"""


def get_1934_model(mode: str = "calibrate") -> Path:
    """Construct the path to a 1934-638 model. This is intended to calibrate
    the bandpass.

    Args:
        mode (str, optional): Calibration software intended to be used. This will determine model file to load. Supported modes are 'calibrate'. Defaults to 'calibrate'.

    Raises:
        ValueError: When supplied 'mode' is not known.

    Returns:
        Path: Path to 1934-638 calibration model.
    """
    if mode not in KNOWN_1934_FILES.keys():
        logger.info(f"No 1934-638 model available for {mode=}.")
        raise ValueError(
            f"{mode=} not supported. Supported modes {KNOWN_1934_FILES.keys()}"
        )

    logger.info(f"Searching for 1934-638 for {mode=}.")
    model_fn = KNOWN_1934_FILES[mode]
    model_path = get_packaged_resource_path(
        package="flint.data.models", filename=model_fn
    )

    assert model_path.exists(), (
        f"Constructed {model_path} apparently does not exist. Check packaged models. "
    )
    logger.info(f"Calibrate 1934-638 model path: {model_path!s}.")

    return model_path


def generate_gaussian_pb(
    freqs: u.Quantity, aperture: u.Quantity, offset: u.Quantity
) -> GaussianResponse:
    """Calculate the theoretical Gaussian taper for an aperture of
    known size

    Args:
        freqs (u.Quantity): Frequencies to evaluate the beam at
        aperture (u.Quantity): Size of the dish
        offset (u.Quantity): Offset from the centre of the beam

    Returns:
        GaussianResponse: Numerical results of the theoretical gaussian primary beam
    """
    c = 299792458.0 * u.meter / u.second
    solid_angle = 4.0 * np.log(2)

    offset = offset.to(u.rad)
    freqs_hz = freqs.to(u.hertz)
    aperture_m = aperture.to(u.meter)

    fwhms = (c / freqs_hz / aperture_m).decompose() * u.rad

    e = (-offset * offset * solid_angle / (fwhms**2)).decompose()

    taper = np.exp(e)

    return GaussianResponse(freqs=freqs, atten=taper, fwhms=fwhms, offset=offset)


@np.vectorize
def _jinc(x):
    from scipy.special import j1

    if x == 0:
        return 1.0
    return 2 * j1(x) / x


def generate_sinc_squared_pb(
    freqs: u.Quantity, aperture: u.Quantity, offset: u.Quantity
) -> SincSquaredResponse:
    """Calculate the theoretical sinc-squared response of an aperture of
    a known size.

    See Equation 3.78 and 3.79 from:
    https://www.cv.nrao.edu/~sransom/web/Ch3.html

    Args:
        reqs (u.Quantity): Frequencies to evaluate the beam at
        aperture (u.Quantity): Size of the dish
        offset (u.Quantity): Offset from the centre of the beam

    Returns:
        SincSquaredResponse:  Numerical results of the theoretical sinc-squared primary beam
    """
    c = 299792458.0 * u.meter / u.second

    offset = offset.to(u.rad)
    freqs_hz = freqs.to(u.hertz)
    lambda_m = (c / freqs).decompose()

    aperture_m = aperture.to(u.meter)

    fwhms = 0.89 * (c / freqs_hz / aperture_m).decompose() * u.rad

    taper = (
        np.sinc((offset * 0.89 * aperture / lambda_m).decompose()) ** 2
    ).decompose()

    return SincSquaredResponse(freqs=freqs, atten=taper, fwhms=fwhms, offset=offset)


def generate_airy_pb(
    freqs: u.Quantity, aperture: u.Quantity, offset: u.Quantity
) -> AiryResponse:
    """Calculate the theoretical airy response of an aperture of
    a known size.

    Args:
        reqs (u.Quantity): Frequencies to evaluate the beam at
        aperture (u.Quantity): Size of the dish
        offset (u.Quantity): Offset from the centre of the beam

    Returns:
        AiryResponse:  Numerical results of the theoretical sinc-squared primary beam
    """
    c = 299792458.0 * u.meter / u.second

    freqs_hz = freqs.to(u.Hz)
    offset = offset.to(u.rad)
    aperture = 12 * u.m
    lambda_m = (c / freqs).to(u.m)

    k = 2 * np.pi / lambda_m
    power = (
        _jinc(k.value * aperture.to(u.m).value * np.sin(offset.to(u.rad).value / 2))
        ** 2
    )

    fwhms = 1.02 * (c / freqs_hz / aperture).decompose() * u.rad

    return AiryResponse(freqs=freqs_hz, atten=power, fwhms=fwhms, offset=offset)


def generate_pb(
    pb_type: str, freqs: u.Quantity, aperture: u.Quantity, offset: u.Quantity
) -> GaussianResponse | SincSquaredResponse | AiryResponse:
    """Generate the primary beam response using a set of physical quantities. Each
    is assumed to be rotationally invariant, so a 1-D slice can be evaluated.

    Known approximations are:

    * gaussian
    * sincsquared
    * airy

    Args:
        pb_type (str): The type of approximation to use
        freqs (u.Quantity): The frequency to valuate at.
        aperture (u.Quantity): The size of the dish
        offset (u.Quantity): The distance to measure out to

    Raises:
        ValueError: Raised if `pb_type` is not known

    Returns:
        Union[GaussianResponse, SincSquaredResponse, AiryResponse]: Constructed primary beam responses
    """
    response: GaussianResponse | SincSquaredResponse | AiryResponse | None = None
    if pb_type.lower() == "gaussian":
        response = generate_gaussian_pb(freqs=freqs, aperture=aperture, offset=offset)
    elif pb_type.lower() == "sincsquared":
        response = generate_sinc_squared_pb(
            freqs=freqs, aperture=aperture, offset=offset
        )
    elif pb_type.lower() == "airy":
        response = generate_airy_pb(freqs=freqs, aperture=aperture, offset=offset)

    if response is None:
        raise ValueError(f"{pb_type=} is unknown. Available modes are {KNOWN_PB_TYPES}")

    return response


def curved_power_law(
    nu: np.ndarray, norm: float, alpha: float, beta: float, ref_nu: float
) -> np.ndarray:
    """A curved power law model.

    >>> S_nu = S_nu_0 * (nu/nu_0)**alpha * exp(beta*ln(nu/nu_0)**2.)

    Note that in the case of beta=0. the model reduces to a normal power-law.

    Args:
        nu (np.ndarray): Frequency array.
        norm (float): Reference flux.
        alpha (float): Spectral index.
        beta (float): Spectral curvature.
        ref_nu (float): Reference frequency.

    Returns:
        np.ndarray: Model flux.
    """
    x = nu / ref_nu
    c = np.exp(beta * np.log(x) ** 2)

    return norm * x**alpha * c


def fit_curved_pl(freqs: u.Quantity, flux: u.Quantity, ref_nu: u.Quantity) -> CurvedPL:
    """Fit some specified set of datapoints with a generic
    curved powerlaw. This is _not_ meant for real data, ratther
    as a way of representing the functional form of a model
    after it has been perturbed by some assumed primary beam.

    Args:
        freqs (np.ndarray): Frequencies corresponding to each brightness
        flux (np.ndarray): Brightness corresponding to each frequency
        ref_nu (float): Reference frequency that the model is set to

    Returns:
        CurvedPL: The fitted parameter results
    """
    # Strip out the Quantity stuff
    freqs = freqs.to(u.Hz).value
    flux = flux.to(u.Jy).value
    ref_nu = ref_nu.to(u.Hz).value

    p0 = (
        np.median(flux),
        np.log(flux[0] / flux[-1]) / np.log(freqs[0] / freqs[-1]),
        0.0,
    )

    curve_pl = partial(curved_power_law, ref_nu=ref_nu)

    p, cov = curve_fit(curve_pl, freqs, flux, p0)

    params = CurvedPL(norm=p[0], alpha=p[1], beta=p[2], ref_nu=ref_nu)

    return params


def evaluate_src_model(freqs: u.Quantity, src_row: Row, ref_nu: u.Quantity) -> u.Jy:
    """Evaluate a SED of an object using its recordded
    Normalisation, alpha and beta components.

    Args:
        freqs (u.Quantity): Frequencies to evaluate
        src_row (Row): Source propertieis from which the parameters are extracted
        ref_nu (u.Quantity): Reference frequency of the model parameterization

    Returns:
        u.Jy: Brightness of model evaluated across frequency
    """

    fluxes = curved_power_law(
        nu=freqs.to(u.Hz).value,
        norm=src_row["flux"].to(u.Jy).value,
        alpha=src_row["alpha"],
        beta=src_row["beta"],
        ref_nu=ref_nu.to(u.Hz).value,
    )

    return fluxes * u.Jy


def get_known_catalogue(cata: str) -> Catalogue:
    """Get the parameters of a known catalogue

    TODO: Replace with configuration based method to load known cata

    Args:
        cata (str): The lookup name of the catalogue

    Returns:
        Catalogue: properties of known catalogue
    """
    assert cata.upper() in KNOWN_CATAS.keys(), (
        f"'{cata}' not a known catalogue. Acceptable keys are: {KNOWN_CATAS.keys()}."
    )

    cata_info = KNOWN_CATAS[cata.upper()]
    logger.info(f"Loading {cata}={cata_info.file_name}")

    return cata_info


def load_catalogue(
    catalogue_dir: Path,
    catalogue: str | None = None,
    ms_pointing: SkyCoord | None = None,
    assumed_alpha: float = -0.83,
    assumed_beta: float = 0.0,
) -> tuple[Catalogue, Table]:
    """Load in a catalogue table given a name or measurement set declinattion.

    Args:
        catalogue_dir (Path): Directory containing known catalogues
        catalogue (Optional[str], optional): Catalogue name to look up from known catalogues. Defaults to None.
        ms_pointing (Optional[SkyCoord], optional): Pointing direction of the measurement set. Defaults to None.
        assumed_alpha (float, optional): The assumed spectral index to use if there is no spectral index column known in model catalogue. Defaults to -0.83.
        assumed_beta (float, optional): The assumed curvature to use if there is no curvature column known in model catalogue. Defaults to 0.0.

    Raises:
        FileNotFoundError: Raised when a catalogue can not be resolved.

    Returns:
        Tuple[Catalogue,Table]: The `Catalogue` information and `Table` of components loaded
    """
    assert catalogue is not None or ms_pointing is not None, (
        "Either catalogue or dec_point have to be provided. "
    )

    if catalogue:
        logger.info(f"Loading provided catalogue {catalogue=}")
        cata = get_known_catalogue(catalogue)

    else:
        # Assertion is done to keep the linters happy
        assert ms_pointing is not None, "Expected SkyCoord object, received None. "
        dec_point = float(ms_pointing.dec.deg)
        logger.info(f"Automatically loading catalogue based on {dec_point=:.2f}")

        if dec_point < -75.0:
            cata = get_known_catalogue("SUMSS")
        elif dec_point < 26.0:
            cata = get_known_catalogue("RACSLOW")
        else:
            cata = get_known_catalogue("NVSS")

    cata_path = catalogue_dir / cata.file_name

    if not cata_path.exists():
        raise FileNotFoundError(f"Catalogue {cata_path} not found.")

    cata_tab = Table.read(cata_path)
    logger.info(f"Loaded table, found {len(cata_tab)} sources. ")

    return _fill_default_sed_columns(
        catalogue=cata,
        table=cata_tab,
        assumed_alpha=assumed_alpha,
        assumed_beta=assumed_beta,
    )


def _fill_default_sed_columns(
    catalogue: Catalogue, table: Table, assumed_alpha: float, assumed_beta: float
) -> tuple[Catalogue, Table]:
    """Add default spectral-index/curvature columns to `table` for whichever
    of `catalogue`'s alpha_col/beta_col are not set, returning a `Catalogue`
    pointing at them.

    Args:
        catalogue (Catalogue): Catalogue description, possibly missing alpha_col/beta_col
        table (Table): The loaded catalogue table
        assumed_alpha (float): Default spectral index to fill in if alpha_col is None
        assumed_beta (float): Default curvature to fill in if beta_col is None

    Returns:
        tuple[Catalogue, Table]: Updated catalogue description and table
    """
    cols = catalogue._asdict()
    if catalogue.alpha_col is None:
        logger.info(
            f"No 'alpha' column, adding default spectral index of {assumed_alpha:.3f}. "
        )
        table["alpha"] = assumed_alpha
        cols["alpha_col"] = "alpha"
    if catalogue.beta_col is None:
        logger.info(f"No 'beta' column, adding default {assumed_beta:.3f}. ")
        table["beta"] = assumed_beta
        cols["beta_col"] = "beta"

    return Catalogue(**cols), table


def _deconvolve_catalogue_shapes(
    table: Table,
    maj_col: str,
    min_col: str,
    pa_col: str,
    psf_maj_col: str,
    psf_min_col: str,
    psf_pa_col: str,
    shape_unit: u.Unit,
) -> None:
    """Deconvolve as-observed (PSF-convolved) source sizes by their per-row
    PSF, overwriting `maj_col`/`min_col`/`pa_col` in place with the intrinsic
    sizes. A source whose PSF is not smaller than its observed size in every
    direction is treated as unresolved (point-like), rather than raising.

    Args:
        table (Table): Table containing the columns to deconvolve, modified in place
        maj_col (str): Column of as-observed major-axis sizes
        min_col (str): Column of as-observed minor-axis sizes
        pa_col (str): Column of as-observed position angles
        psf_maj_col (str): Column of per-source PSF major-axis sizes
        psf_min_col (str): Column of per-source PSF minor-axis sizes
        psf_pa_col (str): Column of per-source PSF position angles
        shape_unit (u.Unit): Unit that maj_col/min_col/psf_maj_col/psf_min_col are in
    """
    from radio_beam import Beam

    new_maj = np.zeros(len(table))
    new_min = np.zeros(len(table))
    new_pa = np.zeros(len(table))

    for i in range(len(table)):
        observed = Beam(
            major=float(table[maj_col][i]) * shape_unit,
            minor=float(table[min_col][i]) * shape_unit,
            pa=float(table[pa_col][i]) * u.deg,
        )
        psf = Beam(
            major=float(table[psf_maj_col][i]) * shape_unit,
            minor=float(table[psf_min_col][i]) * shape_unit,
            pa=float(table[psf_pa_col][i]) * u.deg,
        )
        deconvolved = observed.deconvolve(psf, failure_returns_pointlike=True)
        new_maj[i] = deconvolved.major.to(shape_unit).value
        new_min[i] = deconvolved.minor.to(shape_unit).value
        new_pa[i] = deconvolved.pa.to(u.deg).value

    table[maj_col] = new_maj * shape_unit
    table[min_col] = new_min * shape_unit
    table[pa_col] = new_pa * u.deg


def load_user_catalogue(sky_model_options: SkyModelOptions) -> tuple[Catalogue, Table]:
    """Load a user-supplied catalogue for the sky model. Column names and
    units are never guessed -- see the `SkyModelOptions.catalogue_*` fields.

    Args:
        sky_model_options (SkyModelOptions): Must have `catalogue_path` set

    Raises:
        ValueError: Raised if a required column is not set

    Returns:
        tuple[Catalogue, Table]: The `Catalogue` information and `Table` of components loaded
    """
    catalogue_path = sky_model_options.catalogue_path
    assert catalogue_path is not None and catalogue_path.exists(), (
        f"{catalogue_path=} must be set and exist"
    )
    if (
        sky_model_options.catalogue_freq is None
        and sky_model_options.catalogue_freq_col is None
    ):
        raise ValueError(
            "One of catalogue_freq or catalogue_freq_col must be set for a "
            "user-supplied catalogue -- never guessed"
        )

    ra_col = sky_model_options.catalogue_ra_col
    dec_col = sky_model_options.catalogue_dec_col
    flux_col = sky_model_options.catalogue_flux_col
    if not (ra_col and dec_col and flux_col):
        raise ValueError(
            "catalogue_ra_col, catalogue_dec_col and catalogue_flux_col must all "
            "be set for a user-supplied catalogue -- these are never guessed"
        )

    shape_cols = (
        sky_model_options.catalogue_maj_col,
        sky_model_options.catalogue_min_col,
        sky_model_options.catalogue_pa_col,
    )
    if any(shape_cols) and not all(shape_cols):
        raise ValueError(
            "catalogue_maj_col/catalogue_min_col/catalogue_pa_col must be all "
            "set or all unset"
        )

    table = Table.read(catalogue_path)
    logger.info(f"Loaded user catalogue {catalogue_path}, found {len(table)} sources. ")

    radec_unit = u.Unit(sky_model_options.catalogue_radec_unit)
    table[ra_col].unit = radec_unit
    table[dec_col].unit = radec_unit
    table[flux_col].unit = u.Unit(sky_model_options.catalogue_flux_unit)

    freq_col = sky_model_options.catalogue_freq_col
    if freq_col is not None:
        table[freq_col].unit = u.Unit(sky_model_options.catalogue_freq_unit)

    name_col = sky_model_options.catalogue_name_col
    if name_col is None:
        name_col = "_flint_src_name"
        table[name_col] = [f"src{i}" for i in range(len(table))]

    if all(shape_cols):
        maj_col, min_col, pa_col = shape_cols
        assert maj_col is not None and min_col is not None and pa_col is not None, (
            "Expected shape columns to be set, received None. "
        )
        shape_unit = u.Unit(sky_model_options.catalogue_shape_unit)
        table[maj_col].unit = shape_unit
        table[min_col].unit = shape_unit
        table[pa_col].unit = u.deg

        if sky_model_options.catalogue_sizes_deconvolved is None:
            raise ValueError(
                "catalogue_sizes_deconvolved must be set (True or False) "
                "whenever catalogue_maj_col is set -- never guessed"
            )
        if not sky_model_options.catalogue_sizes_deconvolved:
            psf_cols = (
                sky_model_options.catalogue_psf_maj_col,
                sky_model_options.catalogue_psf_min_col,
                sky_model_options.catalogue_psf_pa_col,
            )
            if not all(psf_cols):
                raise ValueError(
                    "catalogue_psf_maj_col/catalogue_psf_min_col/catalogue_psf_pa_col "
                    "must all be set when catalogue_sizes_deconvolved is False "
                    "-- there is no pipeline restoring beam to fall back on here"
                )
            psf_maj_col, psf_min_col, psf_pa_col = psf_cols
            assert (
                psf_maj_col is not None
                and psf_min_col is not None
                and psf_pa_col is not None
            ), "Expected PSF columns to be set, received None. "
            table[psf_maj_col].unit = shape_unit
            table[psf_min_col].unit = shape_unit
            table[psf_pa_col].unit = u.deg
            _deconvolve_catalogue_shapes(
                table=table,
                maj_col=maj_col,
                min_col=min_col,
                pa_col=pa_col,
                psf_maj_col=psf_maj_col,
                psf_min_col=psf_min_col,
                psf_pa_col=psf_pa_col,
                shape_unit=shape_unit,
            )
    else:
        maj_col, min_col, pa_col = (
            "_flint_src_maj",
            "_flint_src_min",
            "_flint_src_pa",
        )
        table[maj_col] = np.zeros(len(table)) * u.arcsecond
        table[min_col] = np.zeros(len(table)) * u.arcsecond
        table[pa_col] = np.zeros(len(table)) * u.deg

    catalogue = Catalogue(
        survey="USER",
        file_name=catalogue_path.name,
        freq=sky_model_options.catalogue_freq or 0.0,
        ra_col=ra_col,
        dec_col=dec_col,
        name_col=name_col,
        flux_col=flux_col,
        maj_col=maj_col,
        min_col=min_col,
        pa_col=pa_col,
        alpha_col=sky_model_options.catalogue_alpha_col,
        beta_col=sky_model_options.catalogue_beta_col,
        vizier_id=None,
    )

    return _fill_default_sed_columns(
        catalogue=catalogue,
        table=table,
        assumed_alpha=sky_model_options.assumed_alpha,
        assumed_beta=sky_model_options.assumed_beta,
    )


def preprocess_catalogue(
    cata_info: Catalogue,
    cata_tab: Table,
    ms_pointing: SkyCoord,
    flux_cut: float = 0.02,
    radial_cut: u.deg = 1.0 * u.deg,
    ref_freq_col: str | None = None,
) -> QTable:
    """Apply the flux and separation cuts to a loaded table, and transform input column names to an
    expected set of column names.

    Args:
        cata_info (Catalogue): Description of the catalogue from known catalogues
        cata_tab (Table): The loaded catalogue table
        ms_pointing (SkyCoord): Pointing of the measurement set
        flux_cut (float, optional): Flux cut in Jy. Defaults to 0.02.
        radial_cut (u.deg, optional): Radial separation cut in deg. Defaults to 1..
        ref_freq_col (str | None, optional): A column (already in Hz) to carry through
            as "ref_freq", used as the per-row SED reference frequency instead of
            `cata_info.freq`. Defaults to None.

    Returns:
        QTable: _description_
    """
    # First apply pre-processing options
    flux_mask = cata_tab[cata_info.flux_col] > flux_cut
    logger.info(f"{np.sum(flux_mask)} above {flux_cut} Jy.")

    sky_pos = SkyCoord(cata_tab[cata_info.ra_col], cata_tab[cata_info.dec_col])
    sep_mask = ms_pointing.separation(sky_pos) < radial_cut
    logger.info(f"{np.sum(sep_mask)} sources within {radial_cut.to(u.deg):.3f}.")

    mask = flux_mask & sep_mask
    logger.info(f"{np.sum(sep_mask)} common sources selected. ")

    cata_tab = cata_tab[mask]

    # Rename the columns to a expected form
    cols = [
        cata_info.ra_col,
        cata_info.dec_col,
        cata_info.name_col,
        cata_info.flux_col,
        cata_info.maj_col,
        cata_info.min_col,
        cata_info.pa_col,
        cata_info.alpha_col,
        cata_info.beta_col,
    ]
    out_cols = ["RA", "DEC", "name", "flux", "maj", "min", "pa", "alpha", "beta"]
    if ref_freq_col is not None:
        cols = [*cols, ref_freq_col]
        out_cols = [*out_cols, "ref_freq"]
    new_cata_tab = cata_tab[cols]

    for orig, new in zip(cols, out_cols):
        logger.debug(f"Updating Table column {orig} to {new}.")
        new_cata_tab[orig].name = new

    # Put the columns into expected units
    for key, unit_str in NORM_COLS.items():
        new_cata_tab[key] = new_cata_tab[key].to(u.Unit(unit_str))

    return QTable(new_cata_tab)


def make_ds9_region(out_path: Path, sources: list[Row]) -> Path:
    """Create a DS9 region file of the sky-model derived

    Args:
        out_path (Path): Output path to of the region file to write
        sources (List[Row]): Collection of Row objects (with normalised column names)

    Returns:
        Path: Path to the region file created
    """
    logger.info(
        f"Creating DS9 region file, writing {len(sources)} regions to {out_path!s}."
    )
    with open(out_path, "w") as out_file:
        out_file.write("# DS9 region file\n")
        out_file.write("fk5\n")

        for source in sources:
            if source["maj"] < 1.0 * u.arcsecond and source["min"] < 1.0 * u.arcsecond:
                out_file.write(
                    "point({:f},{:f}) # point=circle color=red dash=1\n".format(
                        source["RA"].value, source["DEC"].value
                    )
                )
            else:
                out_file.write(
                    "ellipse({:f},{:f},{:f},{:f},{:f}) # color=red dash=1\n".format(
                        source["RA"].value,
                        source["DEC"].value,
                        source["maj"].value,
                        source["min"].value,
                        90.0 + source["pa"].value,
                    )
                )

    return out_path


def make_hyperdrive_model(out_path: Path, sources: list[tuple[Row, CurvedPL]]) -> Path:
    """Writes a Hyperdrive sky-model to a yaml file.

    Args:
        out_path (Path): The output path that the sky-model would be written to
        sources (List[Tuple[Row,CurvedPL]]): Collection of sources to write, including the
        normalized row and the results of fitting to the estimated apparent SED

    Returns:
        Path: The path of the file created
    """
    logger.info(
        f"Creating hyperdrive sky-model, writing {len(sources)} components to {out_path}."
    )
    src_list = {}

    for row, cpl in sources:
        logger.debug(row)

        src_ra = float(row["RA"].to(u.deg).value)
        src_dec = float(row["DEC"].to(u.deg).value)
        comp_type = (
            "point"
            if (row["maj"] < 1.0 * u.arcsecond and row["min"] < 1.0 * u.arcsecond)
            else {
                "gaussian": {
                    "maj": float(row["maj"].to(u.arcsecond).value),
                    "min": float(row["min"].to(u.arcsecond).value),
                    "pa": float(row["pa"].to(u.deg).value),
                }
            }
        )
        flux_type = {
            "curved_power_law": {
                "si": float(cpl.alpha),
                "q": float(cpl.beta),
                "fd": {"freq": float(cpl.ref_nu), "i": float(cpl.norm)},
            }
        }

        src_list[row["name"]] = [
            {
                "ra": src_ra,
                "dec": src_dec,
                "comp_type": comp_type,
                "flux_type": flux_type,
            }
        ]

    with open(out_path, "w") as out_file:
        yaml.dump(src_list, stream=out_file)

    return out_path


def make_calibrate_model(out_path: Path, sources: list[tuple[Row, CurvedPL]]) -> Path:
    """Create a sky-model file that is compatible with the AO Calibrate software

    Args:
        out_path (Path): Output path of the model file
        sources (List[Tuple[Row,CurvedPL]]): The sources and their (apparent) SED to write

    Returns:
        Path: Output path of the model file
    """
    logger.info(
        f"Creating AO calibrate sky-model, writing {len(sources)} components to {out_path}."
    )

    ref_nu = sources[0][1].ref_nu
    with open(out_path, "w") as out_file:
        out_file.write(
            f"Format = Name, Type, Ra, Dec, I, SpectralIndex, LogarithmicSI, ReferenceFrequency='{ref_nu}', MajorAxis, MinorAxis, Orientation\n"
        )

        for src_row, src_cpl in sources:
            pos = SkyCoord(src_row["RA"], src_row["DEC"])
            ra_dec = pos.to_string(style="hmsdms", sep=":").split()
            ra_str = ra_dec[0]
            # The AO dec string format is '.' delimited, even for the seconds.
            dec_str = ra_dec[1].replace(":", ".")

            if (
                src_row["maj"] < 1.0 * u.arcsecond
                and src_row["min"] < 1.0 * u.arcsecond
            ):
                out_file.write(
                    f"{src_row['name']},"
                    f"POINT,"
                    f"{ra_str},"
                    f"{dec_str},"
                    f"{src_cpl.norm},"
                    f"[{src_cpl.alpha},{src_cpl.beta}],"
                    f"true,{ref_nu},,,\n"
                )
            else:
                out_file.write(
                    f"{src_row['name']},"
                    f"GAUSSIAN,"
                    f"{ra_str},"
                    f"{dec_str},"
                    f"{src_cpl.norm},"
                    f"[{src_cpl.alpha},{src_cpl.beta}],"
                    f"true,{ref_nu},"
                    f"{src_row['maj'].to(u.arcsecond).value},"
                    f"{src_row['maj'].to(u.arcsecond).value},"
                    f"{src_row['pa'].to(u.deg).value},\n"
                )

    return out_path


class SkyModelOutputPaths(NamedTuple):
    """Holds the expected names for different type of sky model outputs"""

    hyperdrive_path: Path
    """Path of the hyperdrive style sky catalogue"""
    calibrate_path: Path
    """Path of the calibrate style sky catalogue"""
    region_path: Path
    """Path of the ds9 region file"""


def get_sky_model_output_paths(ms_path: Path) -> SkyModelOutputPaths:
    """Create a set of expected sky model output file paths

    Args:
        ms_path (Path): The base name to construct the names against

    Raises:
        ValueError: If it appears `ms_path` does not point to a measurement set

    Returns:
        SkyModelOutputPaths: The set of paths to use when creating models
    """
    if ms_path.suffix != ".ms":
        message = f"Expecting a measurement set file extension in {ms_path=}"
        raise ValueError(message)

    return SkyModelOutputPaths(
        hyperdrive_path=ms_path.with_suffix(".hyperdrive.yaml"),
        calibrate_path=ms_path.with_suffix(".calibrate.txt"),
        region_path=ms_path.with_suffix(".model.reg"),
    )


def create_sky_model(
    ms_path: Path, sky_model_options: SkyModelOptions, holofile: Path | None = None
) -> SkyModel | None:
    """Create a sky-model to calibrate RACS based measurement sets.

    If no sources were selected then None is returned.

    Args:
        ms_path (Path): Measurement set to create sky-model for
        sky_model_options (SkyModelOptions): Options to use to construct the sky model
        holofile (Path | None, optional): Measured holography cube to sample the primary-beam
            response from. If None, an idealized Gaussian primary beam is used instead. Defaults to None.

    Returns:
        SkyModel | None -- Basic informattion concerning the sky-model derived and the output files. If no sources were selected then None is returned.
    """

    assert ms_path.exists(), f"Measurement set {ms_path} does not exist. "

    beam = extract_beam_from_name(name=ms_path) if holofile is not None else None
    # Fields can be rotated on the sky relative to the holography; alpha is the
    # same differential rotation flint uses for linmos's ASKAP_PB.alpha option.
    alpha = (
        compute_pb_rotation_alpha(pol_axis=get_pol_axis_as_rad(ms=ms_path))
        if holofile is not None
        else None
    )

    direction = get_phase_dir_from_ms(ms=ms_path)
    logger.info(
        f"Extracting local sky catalogue centred on {direction.ra.deg} {direction.dec.deg}."
    )

    freqs = get_freqs_from_ms(ms=ms_path) * u.Hz
    logger.info(
        f"Frequency range: {freqs[0] / 1000.0:.3f} MHz - {freqs[-1] / 1000.0:.3f} MHz (centre = {np.mean(freqs / 1000.0):.3f} MHz)"
    )

    # This is used to estimate a frequency-dependent search radius
    pb = generate_gaussian_pb(freqs=freqs, aperture=12.0 * u.m, offset=0 * u.rad)

    radial_cutoff = (
        sky_model_options.fwhm_scale_cutoff * pb.fwhms[0]
    ).decompose()  # The lowest frequency FWHM is largest
    logger.info(f"Radial cutoff = {radial_cutoff.to(u.deg).value:.3f} degrees")

    if sky_model_options.catalogue_path is not None:
        cata_info, cata_tab = load_user_catalogue(sky_model_options=sky_model_options)
    else:
        cata_info, cata_tab = load_catalogue(
            catalogue_dir=sky_model_options.reference_catalogue_directory,
            catalogue=sky_model_options.reference_name,
            ms_pointing=direction,
            assumed_alpha=sky_model_options.assumed_alpha,
            assumed_beta=sky_model_options.assumed_beta,
        )

    # Normalise the per-row SED reference frequency into a single column,
    # whether it is a per-source column from a user catalogue or a single
    # value shared by every source (every known catalogue, or a user
    # catalogue given a single catalogue_freq)
    ref_freq_col = "_flint_ref_freq"
    if sky_model_options.catalogue_path is not None and (
        sky_model_options.catalogue_freq_col is not None
    ):
        cata_tab[ref_freq_col] = cata_tab[sky_model_options.catalogue_freq_col].to(u.Hz)
    else:
        cata_tab[ref_freq_col] = cata_info.freq * u.Hz

    cata_tab = preprocess_catalogue(
        cata_info,
        cata_tab,
        ms_pointing=direction,
        flux_cut=sky_model_options.flux_cutoff,
        radial_cut=radial_cutoff,
        ref_freq_col=ref_freq_col,
    )

    total_flux: u.Jy = 0.0 * u.Jy
    accepted_rows: list[tuple[Row, CurvedPL]] = []

    for i, row in enumerate(cata_tab):
        src_pos = SkyCoord(row["RA"], row["DEC"])
        src_sep = src_pos.separation(direction)

        # Get the primary beam response, preferring the measured holography
        # beam over the idealized Gaussian when available
        if holofile is not None:
            assert beam is not None, "Expected beam to be resolved, received None. "
            atten = sample_beam_attenuation(
                holofile=holofile,
                beam=beam,
                position=src_pos,
                freqs=freqs,
                alpha=alpha,
            )
        else:
            atten = generate_gaussian_pb(
                freqs=freqs, aperture=12.0 * u.m, offset=src_sep
            ).atten

        # Calculate the expected model
        src_model = evaluate_src_model(freqs=freqs, src_row=row, ref_nu=row["ref_freq"])

        # Estimate the apparent model (intrinsic*response), and
        # then numerically fit to it
        predict_model = fit_curved_pl(
            freqs=freqs, flux=src_model * atten, ref_nu=freqs[0]
        )

        if predict_model.norm < sky_model_options.flux_cutoff:
            continue

        accepted_rows.append((row, predict_model))
        total_flux += predict_model.norm * u.Jy

        logger.info(
            f"{len(accepted_rows):05d} Sep={src_sep.to(u.deg):.3f} S_ref={predict_model.norm:.3f} SI={predict_model.alpha:.3f} beta={predict_model.beta:.3f}"
        )

    logger.info(
        f"\nCreated model, total apparent flux = {total_flux:.4f}, no. sources {len(accepted_rows)}.\n"
    )

    if len(accepted_rows) == 0:
        logger.warning("No sources were selected for the model.")
        return None

    sky_model_output_paths = get_sky_model_output_paths(ms_path=ms_path)

    # TODO: What to return? Total flux/no sources? Path to models created?
    return SkyModel(
        flux_jy=total_flux.to(u.Jy).value,
        no_sources=len(accepted_rows),
        hyperdrive_model=(
            make_hyperdrive_model(
                out_path=sky_model_output_paths.hyperdrive_path, sources=accepted_rows
            )
            if sky_model_options.write_hyperdrive_model
            else None
        ),
        calibrate_model=(
            make_calibrate_model(
                out_path=sky_model_output_paths.calibrate_path, sources=accepted_rows
            )
            if sky_model_options.write_calibrate_model
            else None
        ),
        ds9_region=(
            make_ds9_region(
                out_path=sky_model_output_paths.region_path,
                sources=[r[0] for r in accepted_rows],
            )
            if sky_model_options.write_ds9_region
            else None
        ),
    )


def get_parser():
    parser = ArgumentParser(
        description="Create a calibrate compatible sky-model for a given measurement set. "
    )

    parser.add_argument(
        "ms", type=Path, help="Path to the measurement set to create the sky-model for"
    )

    parser = add_options_to_parser(parser=parser, options_class=SkyModelOptions)

    return parser


def cli() -> None:
    import logging

    logger.setLevel(logging.INFO)

    parser = get_parser()

    args = parser.parse_args()

    sky_model_options = create_options_from_parser(
        parser_namespace=args, options_class=SkyModelOptions
    )

    create_sky_model(ms_path=args.ms, sky_model_options=sky_model_options)


if __name__ == "__main__":
    cli()
