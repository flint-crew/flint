"""Bits around testing the convolution utilities"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from flint.convol import (
    BeamShape,
    check_if_cube_fits,
    convolve_images,
    get_cube_common_beam,
)
from flint.naming import Suffix
from flint.utils import get_packaged_resource_path


@pytest.fixture
def image_fits() -> Path:
    image = Path(
        get_packaged_resource_path(
            package="flint.data.tests",
            filename="SB39400.RACS_0635-31.beam0-MFS-subimage_rms.fits",
        )
    )

    return image


@pytest.fixture
def cube_fits(tmpdir) -> Path:
    """Extract some example cubes"""
    tmp_dir = Path(tmpdir)
    cube_dir = Path(tmp_dir / "cubes")
    cube_dir.mkdir(parents=True, exist_ok=True)

    cubes_zip = Path(
        get_packaged_resource_path(
            package="flint.data.tests", filename="sub_cube_fits_examples.zip"
        )
    )
    assert cubes_zip.exists()
    shutil.unpack_archive(cubes_zip, cube_dir)

    return cube_dir


def _write_flint_image(path: Path, bmaj_deg: float) -> Path:
    """Write a small FITS image with a restoring beam in its header"""
    header = fits.Header()
    header["CTYPE1"], header["CTYPE2"] = "RA---SIN", "DEC--SIN"
    header["CRVAL1"], header["CRVAL2"] = 0.0, -45.0
    header["CRPIX1"], header["CRPIX2"] = 32.0, 32.0
    header["CDELT1"], header["CDELT2"] = -2.0 / 3600, 2.0 / 3600
    header["CUNIT1"], header["CUNIT2"] = "deg", "deg"
    header["BUNIT"] = "Jy/beam"
    header["BMAJ"], header["BMIN"], header["BPA"] = bmaj_deg, bmaj_deg, 0.0
    fits.PrimaryHDU(data=np.ones((64, 64), dtype=np.float32), header=header).writeto(
        path
    )
    return path


@pytest.mark.parametrize("bmaj_deg", [0.0, 10.0 / 3600])
def test_convolve_images_suffix_names(tmpdir, bmaj_deg: float) -> None:
    """Convolved images are named by adding suffix fields to the flint name,
    with any trailing components (e.g. the extension) preserved"""
    image = _write_flint_image(
        path=Path(tmpdir) / "SB1234.RACS_0000-00.beam00.round1.i.MFS.image.fits",
        bmaj_deg=bmaj_deg,
    )
    beam_shape = BeamShape(bmaj_arcsec=20.0, bmin_arcsec=20.0, bpa_deg=0.0)

    conv_images = convolve_images(image_paths=[image], beam_shape=beam_shape)
    assert conv_images == [
        Path(tmpdir) / "SB1234.RACS_0000-00.beam00.round1.i.conv.MFS.image.fits"
    ]
    assert conv_images[0].exists()

    conv_images = convolve_images(
        image_paths=[image],
        beam_shape=beam_shape,
        suffix_spec=Suffix(optimal=True, conv=True),
    )
    assert conv_images == [
        Path(tmpdir) / "SB1234.RACS_0000-00.beam00.round1.i.optimal.conv.MFS.image.fits"
    ]
    assert conv_images[0].exists()
    # Nothing left behind from the intermediate beamcon names
    assert not (Path(tmpdir) / f"{image.stem}.conv.fits").exists()


def test_convolve_images_undefined_beam_copies(tmpdir) -> None:
    """An undefined beam shape copies images into place under the suffixed name"""
    image = _write_flint_image(
        path=Path(tmpdir) / "SB1234.RACS_0000-00.beam00.round1.i.MFS.image.fits",
        bmaj_deg=10.0 / 3600,
    )
    beam_shape = BeamShape(bmaj_arcsec=np.nan, bmin_arcsec=np.nan, bpa_deg=np.nan)

    conv_images = convolve_images(image_paths=[image], beam_shape=beam_shape)
    assert conv_images == [
        Path(tmpdir) / "SB1234.RACS_0000-00.beam00.round1.i.conv.MFS.image.fits"
    ]
    assert conv_images[0].exists()


def test_check_if_cube_fits(cube_fits, image_fits):
    """See if the cube fits checker is picking up cubes with 3 axis"""
    fits_files = list(cube_fits.glob("*sub.fits"))
    assert len(fits_files) == 10
    assert all([check_if_cube_fits(fits_file=f) for f in fits_files])

    assert not check_if_cube_fits(fits_file=image_fits)
    assert not check_if_cube_fits(fits_file=Path("ThisDoesNotExist"))


def test_get_cube_common_beam_and_convol_cubes(cube_fits) -> None:
    """Ensure that the common beam functionality of from beamcon_3D. Also test the
    convolution to the cubes, as the initial compute can be expensive"""
    fits_files = list(cube_fits.glob("*sub.fits"))
    assert len(fits_files) == 10

    data = fits.getdata(fits_files[0])
    data_shape = np.squeeze(data).shape  # type: ignore

    beam_list = get_cube_common_beam(cube_paths=fits_files, cutoff=150.0)
    assert len(beam_list) == data_shape[0]
    assert all([isinstance(b, BeamShape) for b in beam_list])


# This can cause thread locks in testing/ Test works. Test passes but
# produces something like the below when pytest wraps up
# =============== 257 passed, 14075 warnings in 526.16s (0:08:46) ================
# Fatal Python error: _enter_buffered_busy: could not acquire lock for <_io.BufferedWriter name='<stderr>'> at interpreter shutdown, possibly due to daemon threads
# Python
# def test_beam_list_convol(cube_fits):
#     # These come from the beam_list above
#     bmaj_arcsec = [
#         14.7,
#         14.5,
#         14.3,
#         14.2,
#         14.0,
#         13.8,
#         13.8,
#         13.5,
#         13.4,
#         32.9,
#         float("nan"),
#         147.5,
#         13.0,
#         12.9,
#         12.9,
#         12.8,
#         12.7,
#         13.0,
#         40.7,
#         12.7,
#         12.3,
#         12.3,
#         12.2,
#         12.1,
#         12.0,
#         11.9,
#         11.9,
#         11.6,
#         11.4,
#         11.6,
#         11.6,
#         11.5,
#         11.4,
#         11.4,
#         11.4,
#         11.3,
#     ]
#     bmin_arcsec = [
#         12.4,
#         12.2,
#         12.1,
#         12.0,
#         11.8,
#         11.7,
#         11.7,
#         11.6,
#         11.4,
#         16.2,
#         float("nan"),
#         63.7,
#         11.0,
#         10.9,
#         10.8,
#         10.7,
#         10.6,
#         10.8,
#         16.6,
#         10.6,
#         10.3,
#         10.2,
#         10.1,
#         10.0,
#         9.9,
#         9.9,
#         9.8,
#         9.7,
#         9.3,
#         9.6,
#         9.5,
#         9.4,
#         9.4,
#         9.3,
#         9.2,
#         9.2,
#     ]
#     bpa_deg = [
#         75.25,
#         75.02,
#         74.82,
#         74.82,
#         75.0,
#         75.01,
#         75.33,
#         76.03,
#         75.43,
#         -21.79,
#         float("nan"),
#         160.53,
#         73.28,
#         73.22,
#         76.36,
#         76.14,
#         76.07,
#         74.09,
#         55.61,
#         76.84,
#         75.9,
#         75.76,
#         75.96,
#         75.1,
#         75.76,
#         75.65,
#         75.66,
#         73.47,
#         71.63,
#         75.29,
#         75.35,
#         75.69,
#         75.11,
#         75.78,
#         75.84,
#         75.89,
#     ]

#     beam_list = [
#         BeamShape(bmaj_arcsec=bmaj, bmin_arcsec=bmin, bpa_deg=bpa)
#         for bmaj, bmin, bpa in zip(bmaj_arcsec, bmin_arcsec, bpa_deg)
#     ]

#     fits_files = list(cube_fits.glob("*sub.fits"))
#     assert len(fits_files) == 10
#     # This appears to make pytest lock up

#     cube_paths = convolve_cubes(
#         cube_paths=fits_files,
#         beam_shapes=beam_list,
#         cutoff=150.0,
#         executor_type="process",
#     )
#     assert all([isinstance(p, Path) for p in cube_paths])
#     assert all([p.exists() for p in cube_paths])
