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
from scipy.optimize import curve_fit

from flint.catalogue import KNOWN_REFERENCE_CATALOGUES, Catalogue
from flint.logging import logger
from flint.ms import get_freqs_from_ms, get_phase_dir_from_ms
from flint.options import BaseOptions, add_options_to_parser, create_options_from_parser
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
    assumed_q: float = 0.0
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


class CurvedPL(NamedTuple):
    """Container for results of a Curved Power Law,

    >>> S_nu = S_nu_0 * (nu/nu_0)**alpha * exp(q*ln(nu/nu_0)**2.)

    Note that in the case of q=0. the model reduces to a normal power-law.

    """

    # TODO: Should these be quantities?
    norm: float
    """The fitted normalisation of the fitted model"""
    alpha: float
    """The fitted spectral index"""
    q: float
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

    >>> S_nu = S_nu_0 * (nu/nu_0)**alpha * exp(q*ln(nu/nu_0)**2.)

    Note that in the case of q=0. the model reduces to a normal power-law.

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

    params = CurvedPL(norm=p[0], alpha=p[1], q=p[2], ref_nu=ref_nu)

    return params


def evaluate_src_model(freqs: u.Quantity, src_row: Row, ref_nu: u.Quantity) -> u.Jy:
    """Evaluate a SED of an object using its recordded
    Normalisation, alpha and q components.

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
        beta=src_row["q"],
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
    assumed_q: float = 0.0,
) -> tuple[Catalogue, Table]:
    """Load in a catalogue table given a name or measurement set declinattion.

    Args:
        catalogue_dir (Path): Directory containing known catalogues
        catalogue (Optional[str], optional): Catalogue name to look up from known catalogues. Defaults to None.
        ms_pointing (Optional[SkyCoord], optional): Pointing direction of the measurement set. Defaults to None.
        assumed_alpha (float, optional): The assumed spectral index to use if there is no spectral index column known in model catalogue. Defaults to -0.83.
        assumed_q (float, optional): The assumed curvature to use if there is no curvature column known in model catalogue. Defaults to 0.0.

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

    _cols = cata._asdict()
    if cata.alpha_col is None:
        logger.info(
            f"No 'alpha' column, adding default spectral index of {assumed_alpha:.3f}. "
        )
        cata_tab["alpha"] = assumed_alpha
        _cols["alpha_col"] = "alpha"
    if cata.q_col is None:
        logger.info(f"No 'q' column, adding default {assumed_q:.3f}. ")
        cata_tab["q"] = assumed_q
        _cols["q_col"] = "q"

    cata = Catalogue(**_cols)

    return (cata, cata_tab)


def preprocess_catalogue(
    cata_info: Catalogue,
    cata_tab: Table,
    ms_pointing: SkyCoord,
    flux_cut: float = 0.02,
    radial_cut: u.deg = 1.0 * u.deg,
) -> QTable:
    """Apply the flux and separation cuts to a loaded table, and transform input column names to an
    expected set of column names.

    Args:
        cata_info (Catalogue): Description of the catalogue from known catalogues
        cata_tab (Table): The loaded catalogue table
        ms_pointing (SkyCoord): Pointing of the measurement set
        flux_cut (float, optional): Flux cut in Jy. Defaults to 0.02.
        radial_cut (u.deg, optional): Radial separation cut in deg. Defaults to 1..

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
        cata_info.q_col,
    ]
    out_cols = ["RA", "DEC", "name", "flux", "maj", "min", "pa", "alpha", "q"]
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
                "q": float(cpl.q),
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
        f"Creating AO calibrate sky-model, witing {len(sources)} components to {out_path}."
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
                    f"[{src_cpl.alpha},{src_cpl.q}],"
                    f"true,{ref_nu},,,\n"
                )
            else:
                out_file.write(
                    f"{src_row['name']},"
                    f"GAUSSIAN,"
                    f"{ra_str},"
                    f"{dec_str},"
                    f"{src_cpl.norm},"
                    f"[{src_cpl.alpha},{src_cpl.q}],"
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
    ms_path: Path, sky_model_options: SkyModelOptions
) -> SkyModel | None:
    """Create a sky-model to calibrate RACS based measurement sets.

    If no sources were selected then None is returned.

    Args:
        ms_path (Path): Measurement set to create sky-model for
        sky_model_options (SkyModelOptions): Options to use to construct the sky model

    Returns:
        SkyModel | None -- Basic informattion concerning the sky-model derived and the output files. If no sources were selected then None is returned.
    """

    assert ms_path.exists(), f"Measurement set {ms_path} does not exist. "

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

    cata_info, cata_tab = load_catalogue(
        catalogue_dir=sky_model_options.reference_catalogue_directory,
        catalogue=sky_model_options.reference_name,
        ms_pointing=direction,
        assumed_alpha=sky_model_options.assumed_alpha,
        assumed_q=sky_model_options.assumed_q,
    )
    cata_tab = preprocess_catalogue(
        cata_info,
        cata_tab,
        ms_pointing=direction,
        flux_cut=sky_model_options.flux_cutoff,
        radial_cut=radial_cutoff,
    )

    total_flux: u.Jy = 0.0 * u.Jy
    accepted_rows: list[tuple[Row, CurvedPL]] = []

    for i, row in enumerate(cata_tab):
        src_pos = SkyCoord(row["RA"], row["DEC"])
        src_sep = src_pos.separation(direction)

        # Get the primary beam response
        gauss_taper = generate_gaussian_pb(
            freqs=freqs, aperture=12.0 * u.m, offset=src_sep
        )

        # Calculate the expected model
        src_model = evaluate_src_model(
            freqs=freqs, src_row=row, ref_nu=cata_info.freq * u.Hz
        )

        # Estimate the apparent model (intrinsic*response), and
        # then numerically fit to it
        predict_model = fit_curved_pl(
            freqs=freqs, flux=src_model * gauss_taper.atten, ref_nu=freqs[0]
        )

        if predict_model.norm < sky_model_options.flux_cutoff:
            continue

        accepted_rows.append((row, predict_model))
        total_flux += predict_model.norm * u.Jy

        logger.info(
            f"{len(accepted_rows):05d} Sep={src_sep.to(u.deg):.3f} S_ref={predict_model.norm:.3f} SI={predict_model.alpha:.3f} q={predict_model.q:.3f}"
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
