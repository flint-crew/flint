"""Tests around BANE and aegean, which are one of the source finding
tools used in flint. BANE is also used to derive signal maps that ultimately
feed the clean mask creation.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS

from flint.exceptions import AttemptRerunException
from flint.source_finding.aegean import (
    AegeanOptions,
    BANEOptions,
    _bane_output_callback,
    _get_aegean_command,
    _get_bane_command,
    fft_bane_options_from_bane,
    get_parser,
    run_bane_and_aegean,
)


def test_get_parser():
    """Make sure that there are no conflicting options when creating the parser object"""
    _ = get_parser()


def test_bane_deadlock_callback():
    """Noticed that BANE sometimes goes into a 'deadlock' state, and will
    seemingly always issue an log (from scipy, presumably, arrr)
    around interpolation not being strictly ascending"""

    lines = (
        "30146:INFO using 8 cores",
        "30146:INFO using 7 stripes",
        "30343:WARNING The points in dimension 0 must be strictly ascending or descending",
    )
    _bane_output_callback(line=lines[0])
    _bane_output_callback(line=lines[1])

    with pytest.raises(AttemptRerunException):
        _bane_output_callback(line=lines[2])


def test_bane_options():
    bane_opts = BANEOptions(box_size=(2, 1), grid_size=(20, 10))

    assert isinstance(bane_opts, BANEOptions)

    bane_str = _get_bane_command(
        image=Path("this/is/a/test.fits"), bane_options=bane_opts
    )

    assert isinstance(bane_str, str)

    expected = f"BANE this/is/a/test.fits --cores {bane_opts.cores} --stripes {bane_opts.cores - 1} --grid 20 10 --box 2 1"
    assert expected == bane_str


def test_bane_options_with_defaults():
    bane_opts = BANEOptions(box_size=None, grid_size=None)

    assert isinstance(bane_opts, BANEOptions)

    bane_str = _get_bane_command(
        image=Path("this/is/a/test.fits"), bane_options=bane_opts
    )

    assert isinstance(bane_str, str)

    expected = f"BANE this/is/a/test.fits --cores {bane_opts.cores} --stripes {bane_opts.cores - 1}"
    assert expected == bane_str

    bane_opts = BANEOptions(box_size=(3, 5), grid_size=None)
    bane_str = _get_bane_command(
        image=Path("this/is/a/test.fits"), bane_options=bane_opts
    )
    expected = f"BANE this/is/a/test.fits --cores {bane_opts.cores} --stripes {bane_opts.cores - 1} --box 3 5"
    assert expected == bane_str


def test_aegean_options():
    """Testing basic aegean options creation and expected command"""
    aegean_options = AegeanOptions(nocov=True, maxsummits=4, autoload=True)

    # This are silly tests
    assert isinstance(aegean_options, AegeanOptions)
    assert aegean_options.maxsummits == 4

    ex_path = Path("this/is/a/test.fits")
    aegean_command = _get_aegean_command(
        image=ex_path, base_output="example", aegean_options=aegean_options
    )

    expected_cmd = "aegean this/is/a/test.fits --autoload --nocov --maxsummits 4 --table example.fits"
    assert aegean_command == expected_cmd

    aegean_options2 = AegeanOptions(nocov=False, maxsummits=40, autoload=False)
    aegean_command2 = _get_aegean_command(
        image=ex_path, base_output="example", aegean_options=aegean_options2
    )

    expected_cmd2 = "aegean this/is/a/test.fits --maxsummits 40 --table example.fits"
    assert aegean_command2 == expected_cmd2


@pytest.mark.parametrize(
    "grid_size, box_size, expected",
    [
        ((4, 4), (196, 196), (4, 49)),
        ((2, 2), (196, 196), (2, 98)),
        ((0, 0), (196, 196), (0, 196)),
        ((4, 4), None, (4, None)),
        (None, (196, 196), (None, None)),
    ],
)
def test_fft_bane_options_from_bane(grid_size, box_size, expected, caplog):
    """The grid becomes the FFT BANE's step and the box keeps its size in image
    pixels. A step of 0 is no downsampling, and unset sizes are left to the beam"""
    with caplog.at_level(logging.WARNING):
        fft_options = fft_bane_options_from_bane(
            bane_options=BANEOptions(grid_size=grid_size, box_size=box_size)
        )

    assert (fft_options.step_size, fft_options.box_size) == expected
    assert "Setting the FFT BANE step" not in caplog.text


def test_fft_bane_options_from_bane_caps_the_step(caplog):
    """A grid above 4 is set to a step of 4, with a warning, and the box is kept the
    same size in image pixels"""
    with caplog.at_level(logging.WARNING):
        fft_options = fft_bane_options_from_bane(bane_options=BANEOptions())

    assert (fft_options.step_size, fft_options.box_size) == (4, 49)
    assert "Setting the FFT BANE step to 4 rather than the grid of 16" in caplog.text
    # Pixels are rejected at 3 sigma as BANE does, with the kernel closest to its box
    assert (fft_options.clip_sigma, fft_options.kernel) == (3.0, "tophat")

    fft_options = fft_bane_options_from_bane(bane_options=BANEOptions(), max_step=8)
    assert (fft_options.step_size, fft_options.box_size) == (8, 24)


def test_fft_bane_options_from_bane_needs_square_sizes():
    """The FFT BANE takes a single step and box size"""
    with pytest.raises(ValueError, match="square grid_size"):
        fft_bane_options_from_bane(bane_options=BANEOptions(grid_size=(16, 8)))
    with pytest.raises(ValueError, match="square box_size"):
        fft_bane_options_from_bane(bane_options=BANEOptions(box_size=(196, 98)))


def _beam_image(path: Path, size: int = 512) -> Path:
    """Noise with a beam and WCS, as a degenerate (stokes, freq) wsclean image"""
    wcs = WCS(naxis=2)
    wcs.wcs.ctype = ["RA---SIN", "DEC--SIN"]
    wcs.wcs.crval = [180.0, -30.0]
    wcs.wcs.crpix = [size / 2, size / 2]
    wcs.wcs.cdelt = [-1 / 3600, 1 / 3600]
    header = wcs.to_header()
    header["BMAJ"] = header["BMIN"] = 10 / 3600
    header["BPA"] = 0.0
    data = np.random.default_rng(0).normal(0, 1e-3, (1, 1, size, size))
    fits.writeto(path, data.astype(np.float32), header)
    return path


def test_run_bane_and_aegean_with_the_fft_bane(tmp_path, monkeypatch):
    """With flint_use_fft the maps aegean autoloads are written by flint, on the image's
    grid, and only aegean runs in the container"""
    commands = []
    monkeypatch.setattr(
        "flint.source_finding.aegean.run_singularity_command",
        lambda image, command, **kwargs: commands.append(command),
    )
    image = _beam_image(tmp_path / "SB1.RACS_0000+00.beam00.i.MFS.image.fits")

    outputs = run_bane_and_aegean(
        image=image,
        aegean_container=tmp_path / "aegean.sif",
        bane_options=BANEOptions(flint_use_fft=True),
    )

    assert len(commands) == 1 and commands[0].startswith("aegean ")
    assert outputs.bkg == tmp_path / "SB1.RACS_0000+00.beam00.i.MFS.image_bkg.fits"
    assert outputs.rms == tmp_path / "SB1.RACS_0000+00.beam00.i.MFS.image_rms.fits"
    for path in (outputs.bkg, outputs.rms):
        assert fits.getdata(path).shape == fits.getdata(image).shape
        assert WCS(fits.getheader(path)).celestial.wcs.compare(
            WCS(fits.getheader(image)).celestial.wcs
        )
    assert np.nanmedian(fits.getdata(outputs.rms)) == pytest.approx(1e-3, rel=0.3)
