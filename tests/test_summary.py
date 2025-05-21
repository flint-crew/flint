from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from astropy.coordinates import EarthLocation, Latitude, Longitude
from astropy.time import Time

from flint.imager.wsclean import ImageSet
from flint.ms import get_telescope_location_from_ms, get_times_from_ms
from flint.source_finding.aegean import AegeanOutputs
from flint.summary import (
    FieldSummary,
    add_rms_information,
    create_beam_summary,
    create_field_summary,
    update_field_summary,
)
from flint.utils import get_packaged_resource_path
from flint.validation import make_psf_table


@pytest.fixture
def aegean_outputs_example():
    rms = Path(
        get_packaged_resource_path(
            package="flint.data.tests",
            filename="SB39400.RACS_0635-31.beam0-MFS-subimage_rms.fits",
        )
    )

    comp = Path(
        get_packaged_resource_path(
            package="flint.data.tests",
            filename="SB38959.RACS_1357-18.noselfcal.linmos_comp.fits",
        )
    )

    aegean_outputs = AegeanOutputs(
        bkg=rms, rms=rms, comp=comp, beam_shape=(1.0, 1.0, 1.0), image=rms
    )

    return aegean_outputs


def test_create_beam_summary(ms_example, aegean_outputs_example):
    beam_summary = create_beam_summary(ms=ms_example, components=aegean_outputs_example)

    assert beam_summary.ms_summary.path == ms_example


def test_create_field_summary_beam_summary(ms_example, aegean_outputs_example):
    cal_sbid_path = Path("/scratch3/gal16b/split/39433/SB39433.1934-638.beam0.ms")
    mss = [ms_example for _ in range(36)]

    image_set = ImageSet(prefix="Example", image=[aegean_outputs_example.rms])

    beam_summaries = [
        create_beam_summary(
            ms=ms_example, image_set=image_set, components=aegean_outputs_example
        )
        for _ in range(36)
    ]

    field_summary = create_field_summary(
        mss=mss, cal_sbid_path=cal_sbid_path, beam_summaries=beam_summaries
    )

    assert len(field_summary.beam_summaries) == 36


def test_create_field_summary_beam_summary_nocalid(ms_example, aegean_outputs_example):
    """Make sure the summary will work when an empty of non-existent calid is passed in,
    which may happen for MSs downloaded through casda"""
    mss = [ms_example for _ in range(36)]

    image_set = ImageSet(prefix="Example", image=[aegean_outputs_example.rms])

    beam_summaries = [
        create_beam_summary(
            ms=ms_example, image_set=image_set, components=aegean_outputs_example
        )
        for _ in range(36)
    ]

    for item in (None, "None", "none"):
        field_summary = create_field_summary(
            mss=mss, cal_sbid_path=item, beam_summaries=beam_summaries
        )
        assert field_summary.beam_summaries is not None
        assert len(field_summary.beam_summaries) == 36


def test_field_summary_beam_summary_make_psf(
    ms_example, aegean_outputs_example, tmpdir
):
    cal_sbid_path = Path("/scratch3/gal16b/split/39433/SB39433.1934-638.beam0.ms")
    mss = [ms_example for _ in range(36)]
    image_set = ImageSet(prefix="Example", image=[aegean_outputs_example.rms])

    beam_summaries = [
        create_beam_summary(
            ms=ms_example, image_set=image_set, components=aegean_outputs_example
        )
        for _ in range(36)
    ]

    field_summary = create_field_summary(
        mss=mss, cal_sbid_path=cal_sbid_path, beam_summaries=beam_summaries
    )

    make_psf_table(field_summary=field_summary, output_path=Path(tmpdir))


def test_field_summary(ms_example):
    cal_sbid_path = Path("/scratch3/gal16b/split/39433/SB39433.1934-638.beam0.ms")
    mss = [ms_example for _ in range(36)]

    field_summary = create_field_summary(mss=mss, cal_sbid_path=cal_sbid_path)

    assert isinstance(field_summary, FieldSummary)
    assert field_summary.sbid == "39400"
    assert field_summary.cal_sbid == "39433"

    field_summary = create_field_summary(mss=mss)

    assert isinstance(field_summary, FieldSummary)
    assert field_summary.sbid == "39400"
    assert field_summary.cal_sbid == "None"
    assert isinstance(field_summary.location, EarthLocation)
    assert field_summary.integration_time == 19.90655994415036
    assert isinstance(field_summary.ms_times, Time)
    assert len(field_summary.ms_times) == 2


def test_field_summary_with_mss(ms_example, aegean_outputs_example):
    cal_sbid_path = Path("/scratch3/gal16b/split/39433/SB39433.1934-638.beam0.ms")

    mss = [ms_example for _ in range(36)]

    field_summary = create_field_summary(
        cal_sbid_path=cal_sbid_path,
        aegean_outputs=aegean_outputs_example,
        mss=mss,
    )

    # This si the phase direction, in degrees, of the one MS
    # this pirate is sneakily repeating
    centre = field_summary.centre
    assert np.isclose(centre.ra.deg, 98.211959)  # type: ignore
    assert np.isclose(centre.dec.deg, -30.86099889)  # type: ignore

    assert isinstance(field_summary.hour_angles, Longitude)
    assert isinstance(field_summary.elevations, Latitude)


def test_field_summary_rms_info(ms_example, aegean_outputs_example):
    cal_sbid_path = Path("/scratch3/gal16b/split/39433/SB39433.1934-638.beam0.ms")

    mss = [ms_example for _ in range(36)]

    field_summary = create_field_summary(mss=mss, cal_sbid_path=cal_sbid_path)

    assert isinstance(field_summary, FieldSummary)
    assert field_summary.sbid == "39400"
    assert field_summary.cal_sbid == "39433"
    assert isinstance(field_summary.location, EarthLocation)
    assert field_summary.integration_time == 19.90655994415036
    assert isinstance(field_summary.ms_times, Time)

    field_summary = add_rms_information(
        field_summary=field_summary, aegean_outputs=aegean_outputs_example
    )
    assert field_summary.no_components == 7225


def test_field_summary_rms_info_creation(ms_example, aegean_outputs_example):
    cal_sbid_path = Path("/scratch3/gal16b/split/39433/SB39433.1934-638.beam0.ms")

    mss = [ms_example for _ in range(36)]

    field_summary = create_field_summary(
        mss=mss, cal_sbid_path=cal_sbid_path, aegean_outputs=aegean_outputs_example
    )

    assert isinstance(field_summary, FieldSummary)
    assert field_summary.sbid == "39400"
    assert field_summary.cal_sbid == "39433"
    assert isinstance(field_summary.location, EarthLocation)
    assert field_summary.integration_time == 19.90655994415036
    assert isinstance(field_summary.ms_times, Time)


def test_field_summary_update(ms_example, aegean_outputs_example):
    cal_sbid_path = Path("/scratch3/gal16b/split/39433/SB39433.1934-638.beam0.ms")

    mss = [ms_example for _ in range(36)]

    field_summary = create_field_summary(mss=mss, cal_sbid_path=cal_sbid_path)

    assert isinstance(field_summary, FieldSummary)
    assert field_summary.sbid == "39400"
    assert field_summary.cal_sbid == "39433"
    assert isinstance(field_summary.location, EarthLocation)
    assert field_summary.integration_time == 19.90655994415036
    assert isinstance(field_summary.ms_times, Time)
    assert field_summary.round is None

    field_summary = update_field_summary(
        field_summary=field_summary, aegean_outputs=aegean_outputs_example, round=2
    )
    assert field_summary.no_components == 7225
    assert field_summary.round == 2


def test_ms_example_times(ms_example):
    times = get_times_from_ms(ms=ms_example)

    assert isinstance(times, Time)
    assert len(times) == 1998


def test_ms_example_telescope(ms_example):
    telescope = get_telescope_location_from_ms(ms=ms_example)

    assert isinstance(telescope, EarthLocation)
