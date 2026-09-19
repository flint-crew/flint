"""Tests related to components in the yandasoft linmos coadd.
At the moment this is not testing the actual application. Just
some of the helper functions around it.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from flint.coadd.linmos import (
    BoundingBox,
    LinmosOptions,
    LinmosParsetSummary,
    _create_bound_box_plane,
    _get_alpha_linmos_option,
    _get_holography_linmos_options,
    _get_image_weight_plane,
    _linmos_cleanup,
    create_bound_box,
    generate_weights_list_and_files,
    trim_fits_image,
)
from flint.naming import (
    create_linmos_base_path,
    create_linmos_names,
    extract_beam_from_name,
)


def get_lots_of_names_2() -> list[Path]:
    examples = [
        "59058/SB59058.RACS_1626-84.round4.i.ch0285-0286.linmos.fits",
        "59058/SB59058.RACS_1626-84.round4.i.ch0285-0286.linmos.fits",
        "59058/SB59058.RACS_1626-84.round4.i.ch0070-0071.linmos.fits",
        "59058/SB59058.RACS_1626-84.round4.i.ch0142-0143.linmos.fits",
        "59058/SB59058.RACS_1626-84.round4.i.ch0214-0215.linmos.fits",
        "59058/SB59058.RACS_1626-84.round4.i.ch0286-0287.linmos.fits",
        "59058/SB59058.RACS_1626-84.round4.i.ch0071-0072.linmos.fits",
        "59058/SB59058.RACS_1626-84.round4.i.ch0143-0144.linmos.fits",
        "59058/SB59058.RACS_1626-84.round4.i.ch0215-0216.linmos.fits",
        "59058/SB59058.RACS_1626-84.round4.i.ch0287-0288.linmos.fits",
    ]

    return list(map(Path, examples))


def test_create_name_to_linmos_options():
    """Make sure that the base name created can be passed to an
    instance of LinmosOptions."""
    # Seems silly but was burnt before by not testing this incorrect type
    examples = get_lots_of_names_2()
    # These are sanity, pirates trust nothing
    common_names = create_linmos_base_path(input_images=examples)
    expected_common_name = Path("59058/SB59058.RACS_1626-84.round4.i").absolute()

    assert common_names == expected_common_name
    _ = LinmosOptions(base_output_name=common_names)


def test_get_image_weight_plane():
    """The extraction of weights per plane"""
    data = np.arange(100).reshape((10, 10))

    with pytest.raises(AssertionError):
        _get_image_weight_plane(image_data=data, mode="noexists")  # type: ignore

    assert np.isclose(
        0.0016,
        _get_image_weight_plane(image_data=data, mode="mad", stride=1),
        atol=0.0001,
    )
    assert np.isclose(
        0.00120012,
        _get_image_weight_plane(image_data=data, mode="std", stride=1),
        atol=0.0001,
    )

    data = np.arange(100).reshape((10, 10)) * np.nan
    assert _get_image_weight_plane(image_data=data) == 0.0


def create_fits_image(out_path, image_size=(1000, 1000), set_to_nan: bool = True):
    data = np.zeros(image_size)
    data[10:600, 20:500] = 1
    if set_to_nan:
        data[data == 0] = np.nan

    header = fits.header.Header({"CRPIX1": 10, "CRPIX2": 20})

    fits.writeto(out_path, data=data, header=header)


def create_image_cube(out_path):
    data = np.arange(20 * 100).reshape((20, 10, 10))
    header = fits.header.Header({"CRPIX1": 10, "CRPIX2": 20, "CRPIX3": 1})

    fits.writeto(out_path, header=header, data=data)


def test_linmos_alpha_option():
    """Ensure the rotation string supplied to linmos is calculated appropriately"""

    options_str = _get_alpha_linmos_option(pol_axis=None)
    assert options_str == ""

    options_str = _get_alpha_linmos_option(pol_axis=np.deg2rad(-45))
    expected_str = "linmos.primarybeam.ASKAP_PB.alpha = 0.0 # in radians\n"
    assert options_str == expected_str

    with pytest.raises(AssertionError):
        _get_alpha_linmos_option(pol_axis=1234)


def test_cleanup_image_weights_(tmpdir):
    """Remove the weight files that have been created"""
    cube_weight = Path(tmpdir) / "cubeweight"
    cube_weight.mkdir(parents=True, exist_ok=True)
    cube_fits = cube_weight / "cube.fits"

    create_image_cube(out_path=cube_fits)
    weight_file = cube_fits.with_suffix(".weights.txt")
    assert not weight_file.exists()

    weight_paths = generate_weights_list_and_files(image_paths=[cube_fits], mode="mad")
    linmos_parset_summary = LinmosParsetSummary(
        parset_path=Path("JackSparrow.txt"),
        image_paths=tuple([cube_fits]),
        weight_text_paths=weight_paths,
    )
    assert weight_file.exists()
    assert isinstance(linmos_parset_summary, LinmosParsetSummary)
    files_removed = _linmos_cleanup(linmos_parset_summary=linmos_parset_summary)
    assert not weight_file.exists()
    assert len(files_removed) == 1
    assert files_removed[0] == weight_file


def test_get_image_weights(tmpdir):
    """See whether the weights computed per plane in a cube work appropriately"""
    cube_weight = Path(tmpdir) / "cubeweight"
    cube_weight.mkdir(parents=True, exist_ok=True)
    cube_fits = cube_weight / "cube.fits"

    create_image_cube(out_path=cube_fits)
    weight_file = cube_fits.with_suffix(".weights.txt")
    assert not weight_file.exists()

    generate_weights_list_and_files(image_paths=[cube_fits], mode="mad")
    assert weight_file.exists()
    # The file must end with a newline for linmos to work
    lines = weight_file.read_text().split("\n")
    assert len(lines) == 22, f"{lines}"


def test_get_image_weight_with_strides(tmpdir):
    """See whether the weights computed per plane in a cube work appropriately when striding over data"""
    cube_weight = Path(tmpdir) / "cubeweight"
    cube_weight.mkdir(parents=True, exist_ok=True)
    cube_fits = cube_weight / "cube.fits"

    create_image_cube(out_path=cube_fits)
    weight_file = cube_fits.with_suffix(".weights.txt")
    assert not weight_file.exists()

    generate_weights_list_and_files(image_paths=[cube_fits], mode="mad", stride=10)
    assert weight_file.exists()
    # The file must end with a newline for linmos to work
    lines = weight_file.read_text().split("\n")
    assert len(lines) == 22, f"{lines}"


def test_linmos_holo_options(tmpdir):
    holofile = Path(tmpdir) / "testholooptions/holo_file.fits"
    holofile.parent.mkdir(parents=True, exist_ok=True)

    ifile = Path(tmpdir) / "blackpearl.fits"
    ifile.touch()
    ifile.parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(AssertionError):
        _get_holography_linmos_options(holofile=holofile, pol_axis=None)

    assert _get_holography_linmos_options(holofile=None, pol_axis=None) == ""

    with holofile.open("w") as f:
        f.write("test")

    parset = _get_holography_linmos_options(holofile=holofile, pol_axis=None)
    assert "linmos.primarybeam      = ASKAP_PB\n" in parset
    assert "linmos.removeleakage    = false\n" in parset
    assert f"linmos.primarybeam.ASKAP_PB.image = {holofile.absolute()!s}\n" in parset
    assert "linmos.primarybeam.ASKAP_PB.alpha" not in parset

    parset = _get_holography_linmos_options(holofile=holofile, pol_axis=np.deg2rad(-45))
    assert "linmos.primarybeam      = ASKAP_PB\n" in parset
    assert "linmos.removeleakage    = false\n" in parset
    assert f"linmos.primarybeam.ASKAP_PB.image = {holofile.absolute()!s}\n" in parset
    assert "linmos.primarybeam.ASKAP_PB.alpha" in parset

    parset = _get_holography_linmos_options(
        holofile=holofile, remove_leakage=True, pol_axis=np.deg2rad(-45)
    )
    assert "linmos.primarybeam      = ASKAP_PB\n" in parset
    assert "linmos.removeleakage    = true\n" in parset
    assert f"linmos.primarybeam.ASKAP_PB.image = {holofile.absolute()!s}\n" in parset
    assert "linmos.primarybeam.ASKAP_PB.alpha" in parset

    parset = _get_holography_linmos_options(
        holofile=holofile,
        remove_leakage=True,
        pol_axis=np.deg2rad(-45),
        stokesi_images=[
            ifile,
        ],
    )
    from flint.logging import logger

    logger.info(parset)
    assert "linmos.primarybeam      = ASKAP_PB\n" in parset
    assert "linmos.removeleakage    = true\n" in parset
    assert f"linmos.primarybeam.ASKAP_PB.image = {holofile.absolute()!s}\n" in parset
    assert "linmos.primarybeam.ASKAP_PB.alpha" in parset
    assert f"linmos.stokesinames = [{ifile.with_suffix('').as_posix()}]\n" in parset

    with pytest.raises(AssertionError):
        parset = _get_holography_linmos_options(
            holofile=holofile,
            remove_leakage=True,
            pol_axis=np.deg2rad(-45),
            stokesi_images=[
                Path("doesnotexist.fits"),
            ],
        )


def test_trim_fits_while_blanking(tmp_path):
    """Ensure that fits files can be trimmed appropriately based on row/columns with valid pixels.
    This will require pixels with values of 0.0 to be naned"""
    tmp_dir = tmp_path / "imagenan"
    tmp_dir.mkdir()

    out_fits = tmp_dir / "example.fits"

    create_fits_image(out_fits, set_to_nan=False)
    og_hdr = fits.getheader(out_fits)
    assert og_hdr["CRPIX1"] == 10
    assert og_hdr["CRPIX2"] == 20

    trim_fits_image(out_fits)
    trim_hdr = fits.getheader(out_fits)
    trim_data = fits.getdata(out_fits)
    assert trim_hdr["CRPIX1"] == -10
    assert trim_hdr["CRPIX2"] == 10
    assert trim_data.shape == (590, 480)
    assert np.sum(trim_data == 0.0) == 0


def test_trim_fits(tmp_path):
    """Ensure that fits files can be trimmed appropriately based on row/columns with valid pixels"""
    tmp_dir = tmp_path / "image"
    tmp_dir.mkdir()

    out_fits = tmp_dir / "example.fits"

    create_fits_image(out_fits)
    og_hdr = fits.getheader(out_fits)
    assert og_hdr["CRPIX1"] == 10
    assert og_hdr["CRPIX2"] == 20

    trim_fits_image(out_fits)
    trim_hdr = fits.getheader(out_fits)
    trim_data = fits.getdata(out_fits)
    assert trim_hdr["CRPIX1"] == -10
    assert trim_hdr["CRPIX2"] == 10
    assert trim_data.shape == (590, 480)


def test_trim_fits_cube(tmp_path):
    """Ensure that fits files that has cube can be trimmed appropriately based on row/columns with valid pixels"""
    tmp_dir = tmp_path / "cube"
    tmp_dir.mkdir()

    out_fits = tmp_dir / "example.fits"

    cube_size = (12, 1000, 1000)
    data = np.zeros(cube_size)
    data[3, 10:600, 20:500] = 1
    data[data == 0] = np.nan

    header = fits.header.Header({"CRPIX1": 10, "CRPIX2": 20})

    fits.writeto(out_fits, data=data, header=header)

    og_hdr = fits.getheader(out_fits)
    assert og_hdr["CRPIX1"] == 10
    assert og_hdr["CRPIX2"] == 20

    trim_fits_image(out_fits)
    trim_hdr = fits.getheader(out_fits)
    trim_data = fits.getdata(out_fits)
    assert trim_hdr["CRPIX1"] == -10
    assert trim_hdr["CRPIX2"] == 10
    assert trim_data.shape == (12, 590, 480)  # type: ignore


def test_trim_fits_image_matching(tmp_path):
    """See the the bounding box can be passed through for matching to cutout"""

    tmp_dir = Path(tmp_path) / "image_bb_match"
    tmp_dir.mkdir()

    out_fits = tmp_dir / "example.fits"

    create_fits_image(out_fits)
    og_trim = trim_fits_image(out_fits)

    out_fits2 = tmp_dir / "example2.fits"
    create_fits_image(out_fits2)
    og_hdr = fits.getheader(out_fits2)
    assert og_hdr["CRPIX1"] == 10
    assert og_hdr["CRPIX2"] == 20

    trim_fits_image(image_path=out_fits2, bounding_box=og_trim.bounding_box)
    trim_hdr = fits.getheader(out_fits2)
    trim_data = fits.getdata(out_fits2)
    assert trim_hdr["CRPIX1"] == -10
    assert trim_hdr["CRPIX2"] == 10
    assert trim_data.shape == (590, 480)

    out_fits2 = tmp_dir / "example3.fits"
    create_fits_image(out_fits2, image_size=(300, 300))

    with pytest.raises(ValueError):
        trim_fits_image(image_path=out_fits2, bounding_box=og_trim.bounding_box)


def test_bounding_box():
    """Create a bounding box around a region of valid non-nan/inf pixels"""
    data = np.zeros((1000, 1000))
    data[10:600, 20:500] = 1
    data[data == 0] = np.nan

    bb = create_bound_box(image_data=data)

    assert isinstance(bb, BoundingBox)
    assert bb.xmin == 10
    assert bb.xmax == 600  # the maximum is exclusive, i.e. slice ready
    assert bb.ymin == 20
    assert bb.ymax == 500  # the maximum is exclusive, i.e. slice ready


def test_bounding_box_none():
    """Return None if there are no valid pixels to create a bounding box around"""
    data = np.zeros((1000, 1000)) * np.nan

    bb = _create_bound_box_plane(image_data=data)
    assert bb is None

    bb = create_bound_box(image_data=data)
    assert isinstance(bb, BoundingBox)
    assert bb.xmin == 0
    assert bb.xmin == 0
    assert bb.xmax == 1000
    assert bb.ymax == 1000


def test_bounding_box_cube():
    """Cube cut bounding boxes."""
    data = np.zeros((3, 1000, 1000))
    data[:, 10:600, 20:500] = 1
    data[data == 0] = np.nan

    with pytest.raises(AssertionError):
        _create_bound_box_plane(image_data=data)

    bb = create_bound_box(image_data=data)
    assert isinstance(bb, BoundingBox)
    assert bb.xmin == 10
    assert bb.xmax == 600
    assert bb.ymin == 20
    assert bb.ymax == 500


def test_bounding_box_cube_different_bounds():
    """Cube cut bounding boxes, where the largest bounding box that
    captures all valid pixels"""
    data = np.zeros((3, 1000, 1000))
    data[0, 10:600, 20:500] = 1
    data[1, 100:200, 600:800] = 1
    data[2, 800:888, 20:500] = 1

    data[data == 0] = np.nan

    with pytest.raises(AssertionError):
        _create_bound_box_plane(image_data=data)

    bb = create_bound_box(image_data=data)
    assert isinstance(bb, BoundingBox)
    assert bb.xmin == 10
    assert bb.xmax == 888
    assert bb.ymin == 20
    assert bb.ymax == 800


def test_bounding_box_with_mask():
    """Create a bounding box where the input is converted to a boolean arrau"""
    data = np.zeros((1000, 1000))
    data[10:600, 20:500] = 1

    bb = create_bound_box(image_data=data, is_masked=True)

    assert isinstance(bb, BoundingBox)
    assert bb.xmin == 10
    assert bb.xmax == 600  # the maximum is exclusive, i.e. slice ready
    assert bb.ymin == 20
    assert bb.ymax == 500  # the maximum is exclusive, i.e. slice ready


def write_beam_image(path: Path, ra_deg: float, dec_deg: float) -> Path:
    """A tiny image whose reference direction is the one given."""
    header = fits.Header(
        {
            "CTYPE1": "RA---SIN",
            "CTYPE2": "DEC--SIN",
            "CRVAL1": ra_deg,
            "CRVAL2": dec_deg,
            "CRPIX1": 2.0,
            "CRPIX2": 2.0,
            "CDELT1": -1.5 / 3600.0,
            "CDELT2": 1.5 / 3600.0,
        }
    )
    fits.writeto(path, np.zeros((4, 4), dtype=np.float32), header=header)
    return path


def closepack36_offsets(pitch_deg: float = 0.9) -> list[tuple[float, float]]:
    """Beam offsets of closepack36, in beam order. Beam 0 is a corner beam."""
    x0 = 3 * pitch_deg - pitch_deg / (2 * np.sqrt(2))
    y0 = -pitch_deg / (2 * np.sqrt(2)) - np.sqrt(3) * pitch_deg
    offsets = []
    for row in range(6):
        y = y0 + row * pitch_deg * np.sqrt(3) / 2
        for col in range(6):
            offsets.append((x0 - (col if row % 2 == 0 else col + 0.5) * pitch_deg, y))
    mean_x = float(np.mean([offset[0] for offset in offsets]))
    mean_y = float(np.mean([offset[1] for offset in offsets]))
    return [(x - mean_x, y - mean_y) for x, y in offsets]


def square36_offsets(pitch_deg: float = 0.9) -> list[tuple[float, float]]:
    """Beam offsets of square_6x6, in beam order. Beam 0 is near the centre."""
    layout = [
        31,
        30,
        29,
        28,
        27,
        26,
        32,
        13,
        12,
        11,
        10,
        25,
        33,
        14,
        2,
        3,
        9,
        24,
        34,
        15,
        0,
        1,
        8,
        23,
        35,
        4,
        5,
        6,
        7,
        22,
        16,
        17,
        18,
        19,
        20,
        21,
    ]
    offsets: list[tuple[float, float]] = [(0.0, 0.0)] * 36
    for position, beam in enumerate(layout):
        row, col = divmod(position, 6)
        offsets[beam] = ((2.5 - col) * pitch_deg, (row - 2.5) * pitch_deg)
    return offsets


def write_footprint(tmp_path: Path, offsets, centre=(180.0, -45.0)) -> list[Path]:
    """One image per beam, placed at exact angular offsets from a field centre."""
    import astropy.units as u
    from astropy.coordinates import SkyCoord

    origin = SkyCoord(ra=centre[0] * u.deg, dec=centre[1] * u.deg)
    paths = []
    for beam, (east, north) in enumerate(offsets):
        position = origin.directional_offset_by(
            position_angle=np.arctan2(east, north) * u.rad,
            separation=np.hypot(east, north) * u.deg,
        )
        path = tmp_path / f"SB1234.RACS_1200-45.beam{beam:02d}.round4.i.fits"
        paths.append(
            write_beam_image(path, float(position.ra.deg), float(position.dec.deg))
        )
    return paths


def separations_from_centre(images: list[Path]) -> np.ndarray:
    """Angular distance of each image from the mean of their directions."""
    import astropy.units as u
    from astropy.coordinates import SkyCoord

    ras = np.array([fits.getheader(image)["CRVAL1"] for image in images])
    decs = np.array([fits.getheader(image)["CRVAL2"] for image in images])
    directions = SkyCoord(ra=ras * u.deg, dec=decs * u.deg)
    centre = SkyCoord(directions.cartesian.mean(), frame=directions.frame)
    return centre.separation(directions).to(u.deg).value


def parse_parset_list(parset: str, key: str) -> list[str]:
    """The bracketed list a parset key is set to."""
    for line in parset.splitlines():
        if line.split("=")[0].strip() == key:
            return line.split("=", 1)[1].strip().strip("[]").split(",")
    raise KeyError(f"{key} not in parset")


@pytest.mark.parametrize(
    ("offsets_func", "beam_zero_is_central"),
    [(closepack36_offsets, False), (square36_offsets, True)],
)
def test_central_image_order_leads_with_central_image(
    tmp_path, offsets_func, beam_zero_is_central
):
    """The most central image leads, whichever beam number it happens to carry."""
    from flint.coadd.linmos import central_image_order

    images = write_footprint(tmp_path, offsets_func())

    order = central_image_order(images=images)
    separations = separations_from_centre(images)

    assert sorted(order) == list(range(len(images)))
    assert separations[order[0]] == pytest.approx(separations.min(), abs=1e-6)
    assert list(separations[list(order)]) == sorted(separations[list(order)])
    # closepack36 rasters a hex lattice, so its beam 0 sits at a corner
    assert (order[0] == 0) is beam_zero_is_central


def test_central_image_order_is_independent_of_input_order(tmp_path):
    """The same image leads whichever way round they arrive."""
    from flint.coadd.linmos import central_image_order

    images = write_footprint(tmp_path, closepack36_offsets())
    shuffled = list(reversed(images))

    forward = central_image_order(images=images)
    reverse = central_image_order(images=shuffled)

    assert images[forward[0]] == shuffled[reverse[0]]


def test_central_image_order_near_the_pole(tmp_path):
    """A high declination field must not be averaged in RA alone."""
    from flint.coadd.linmos import central_image_order

    images = write_footprint(tmp_path, closepack36_offsets(), centre=(180.0, -84.0))

    order = central_image_order(images=images)
    separations = separations_from_centre(images)

    assert separations[order[0]] == pytest.approx(separations.min(), abs=1e-6)
    assert separations[order[0]] < 1.0


def test_central_image_order_with_a_flagged_beam(tmp_path):
    """Dropping the most central beam promotes the next one, not beam 0."""
    from flint.coadd.linmos import central_image_order

    images = write_footprint(tmp_path, closepack36_offsets())
    dropped = int(np.argmin(separations_from_centre(images)))
    kept = [image for idx, image in enumerate(images) if idx != dropped]

    order = central_image_order(images=kept)
    separations = separations_from_centre(kept)

    assert kept[order[0]] != images[dropped]
    assert separations[order[0]] == pytest.approx(separations.min(), abs=1e-6)


def test_linmos_parset_sets_a_regrid_method(tmp_path):
    """linmos defaults to linear interpolation, which suppresses source peaks."""
    from flint.coadd.linmos import generate_linmos_parameter_set

    images = write_footprint(tmp_path, closepack36_offsets())
    summary = generate_linmos_parameter_set(
        images=images,
        linmos_names=create_linmos_names(name_prefix=str(tmp_path / "field")),
        linmos_options=LinmosOptions(),
    )

    assert "linmos.regrid.method    = cubic" in summary.parset_path.read_text()


def test_linmos_parset_regrid_method_is_configurable(tmp_path):
    """So the change can be compared against products made the old way."""
    from flint.coadd.linmos import generate_linmos_parameter_set

    images = write_footprint(tmp_path, closepack36_offsets())
    summary = generate_linmos_parameter_set(
        images=images,
        linmos_names=create_linmos_names(name_prefix=str(tmp_path / "field")),
        linmos_options=LinmosOptions(regrid_method="linear"),
    )

    assert "linmos.regrid.method    = linear" in summary.parset_path.read_text()


def test_linmos_parset_leads_with_the_central_image(tmp_path):
    """linmos takes the mosaic frame from whichever image is listed first."""
    from flint.coadd.linmos import generate_linmos_parameter_set

    images = write_footprint(tmp_path, closepack36_offsets())
    summary = generate_linmos_parameter_set(
        images=images,
        linmos_names=create_linmos_names(name_prefix=str(tmp_path / "field")),
        linmos_options=LinmosOptions(),
    )
    parset = summary.parset_path.read_text()

    central = images[int(np.argmin(separations_from_centre(images)))]
    names = parse_parset_list(parset, "linmos.names")
    assert names[0] == str(central).replace(".fits", "")
    assert len(names) == len(images)
    # the beam list has to follow the images it describes
    beams = parse_parset_list(parset, "linmos.beams")
    assert beams[0] == str(extract_beam_from_name(central.name))
    assert beams[0] != "0"


def test_linmos_parset_reorders_stokesi_with_the_images(tmp_path):
    """Stokes I images are matched to the inputs by position, so must follow them."""
    from flint.coadd.linmos import generate_linmos_parameter_set

    holofile = tmp_path / "holo.fits"
    fits.writeto(holofile, np.zeros((2, 2), dtype=np.float32))

    offsets = closepack36_offsets()
    qu_dir = tmp_path / "q"
    i_dir = tmp_path / "i"
    qu_dir.mkdir()
    i_dir.mkdir()
    images = write_footprint(qu_dir, offsets)
    stokesi = write_footprint(i_dir, offsets)

    summary = generate_linmos_parameter_set(
        images=images,
        linmos_names=create_linmos_names(name_prefix=str(tmp_path / "field")),
        linmos_options=LinmosOptions(
            holofile=holofile, stokesi_images=stokesi, force_remove_leakage=True
        ),
    )
    parset = summary.parset_path.read_text()

    names = parse_parset_list(parset, "linmos.names")
    stokesi_names = parse_parset_list(parset, "linmos.stokesinames")
    assert len(names) == len(stokesi_names) == len(images)
    # every image keeps the Stokes I image of its own beam alongside it
    for name, stokesi_name in zip(names, stokesi_names):
        assert Path(name).name == Path(stokesi_name).name
