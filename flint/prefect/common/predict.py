"""Prefect tasks around model visibility prediction"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from prefect import unmapped
from prefect.futures import PrefectFuture, PrefectFutureList

from flint.configuration import Strategy, get_options_from_strategy
from flint.exceptions import NoFieldSourceError
from flint.logging import logger
from flint.ms import MS
from flint.options import AddModelSubtractFieldOptions, FieldOptions, RACSAllOptions
from flint.predict.crystalball import CrystalBallOptions
from flint.prefect.caching import task
from flint.prefect.common.imaging import task_gaincal_applycal_ms
from flint.sky_model import SkyModel, SkyModelOptions, create_sky_model


@task
def task_crystalball_to_ms(ms: MS, crystalball_options: CrystalBallOptions) -> MS:
    """Predict model visibilities into a measurement set using a previously constructed
    blackboard sky model. See ``wsclean -save-source-list`. This used the ``crystalball``
    python package, which under the hood taps into the same dask task runner running
    this flow.

    Visibilities are predicted into the MS's ``MODEL_DATA`` column.

    Args:
        ms (MS): The measurement set where model visibilities will be predicted into.
        crystalball_options (CrystalBallOptions): Options around the crystal ball operation

    Returns:
        MS: An updated MS with the model column set
    """
    from prefect_dask import get_dask_client

    from flint.predict.crystalball import crystalball_predict
    from flint.prefect.helpers import enable_loguru_support

    # crystalball uses loguru. We want to try to attach a handler
    enable_loguru_support()

    with get_dask_client(set_as_default=False) as client:
        logger.info("Obtained the Client supporting the DaskTaskRunner.")
        return crystalball_predict(
            ms=ms,
            crystalball_options=crystalball_options,
            dask_client=client,
            output_column="MODEL_DATA",
        )


@task
def task_addmodel_to_ms(
    ms: MS,
    addmodel_subtract_options: AddModelSubtractFieldOptions,
    skymodel: SkyModel | None = None,
) -> MS:
    from flint.imager.wsclean import get_wsclean_output_source_list_path
    from flint.predict.addmodel import AddModelOptions, add_model

    assert addmodel_subtract_options.calibrate_container is not None, (
        f"{addmodel_subtract_options.calibrate_container=}, which should not happen"
    )

    if skymodel is not None:
        assert skymodel.calibrate_model is not None, (
            f"{skymodel.calibrate_model=}, which should not happen"
        )
        addmodel_options = AddModelOptions(
            model_path=skymodel.calibrate_model,
            ms_path=ms.path,
            mode="c",
            datacolumn="MODEL_DATA",
        )
        add_model(
            add_model_options=addmodel_options,
            container=addmodel_subtract_options.calibrate_container,
            remove_datacolumn=True,
        )
        return ms.with_options(model_column="MODEL_DATA")

    logger.info(f"Searching for wsclean source list for {ms.path}")
    for idx, pol in enumerate(addmodel_subtract_options.wsclean_pol_mode):
        wsclean_source_list_path = get_wsclean_output_source_list_path(
            name_path=ms.path, pol=pol
        )
        assert wsclean_source_list_path.exists(), (
            f"{wsclean_source_list_path=} was requested, but does not exist"
        )

        # This should attempt to add model of different polarisations together.
        # But to this point it is a future proof and is not tested.
        addmodel_options = AddModelOptions(
            model_path=wsclean_source_list_path,
            ms_path=ms.path,
            mode="c" if idx == 0 else "a",
            datacolumn="MODEL_DATA",
        )
        add_model(
            add_model_options=addmodel_options,
            container=addmodel_subtract_options.calibrate_container,
            remove_datacolumn=idx == 0,
        )

    return ms.with_options(model_column="MODEL_DATA")


@task
def task_create_sky_model(
    ms: MS,
    sky_model_options: SkyModelOptions,
    holofile: Path | None = None,
) -> SkyModel:
    """Create a sky model for a measurement set from a reference catalogue,
    to be predicted into MODEL_DATA via ``task_addmodel_to_ms``.

    Args:
        ms (MS): The measurement set to create a sky model for.
        sky_model_options (SkyModelOptions): The options to use when creating the sky model.
        holofile (Path | None, optional): Measured holography cube to sample the primary-beam
            response from. If None, falls back to an idealized Gaussian primary beam. Defaults to None.

    Raises:
        NoFieldSourceError: Raised if no catalogue sources are left to model the field

    Returns:
        SkyModel: The derived sky model
    """
    if holofile is None:
        logger.warning(
            f"No holography cube supplied for {ms.path}, falling back to an "
            "idealized Gaussian primary beam"
        )
    elif not holofile.exists():
        raise FileNotFoundError(f"{holofile=} was supplied, but does not exist")

    skymodel = create_sky_model(
        ms_path=ms.path, sky_model_options=sky_model_options, holofile=holofile
    )
    if skymodel is None:
        raise NoFieldSourceError(
            f"No sky-model sources survived the flux and radial cuts for {ms.path}. "
            "Lowering flux_cutoff, raising fwhm_scale_cutoff, or turning on "
            "holography_taper in the skymodel strategy options may help."
        )

    return skymodel


def check_skymodel_calibration_options(
    field_options: FieldOptions | RACSAllOptions,
    holofiles: Sequence[Path | None],
) -> None:
    """Raise ``ValueError`` should anything the sky-model calibration round needs be missing."""
    # The catalogues themselves are verified with the rest of each flow's options
    if field_options.reference_catalogue_directory is None:
        raise ValueError(
            "reference_catalogue_directory needs to be set if run_skymodel_calibration is True"
        )
    if field_options.calibrate_container is None:
        raise ValueError(
            "calibrate_container needs to be set if run_skymodel_calibration is True"
        )
    if field_options.casa_container is None:
        raise ValueError(
            "casa_container needs to be set if run_skymodel_calibration is True"
        )
    for holofile in holofiles:
        if holofile is not None and not holofile.exists():
            raise ValueError(
                f"Holography cube {holofile} is set for sky-model calibration, but does not exist"
            )


def get_skymodel_gaincal_options(strategy: Strategy | None) -> dict[str, Any]:
    """Gaincal options for the sky-model calibration round: the strategy's round 0, always phase-only."""
    options = get_options_from_strategy(
        strategy=strategy, mode="gaincal", round_info=0, operation="selfcal"
    )
    if options.get("calmode", "p") != "p":
        logger.warning(
            f"Ignoring calmode={options['calmode']!r} for the sky-model calibration round, which is phase-only"
        )
    return {**options, "calmode": "p"}


def calibrate_mss_against_sky_model(
    mss: Sequence[PrefectFuture[MS] | MS],
    holofiles: Sequence[Path | None],
    strategy: Strategy | None,
    field_options: FieldOptions | RACSAllOptions,
) -> PrefectFutureList[MS]:
    """Phase-only calibrate each MS against a sky-model predicted from a reference
    catalogue, as self-calibration round 0. Each MS is paired with the holography
    cube at the same position in ``holofiles``."""
    assert field_options.reference_catalogue_directory is not None, (
        "reference_catalogue_directory is required for sky-model calibration"
    )
    sky_model_options = SkyModelOptions(
        reference_catalogue_directory=field_options.reference_catalogue_directory,
        write_calibrate_model=True,
    ).with_options(
        **get_options_from_strategy(
            strategy=strategy, mode="skymodel", round_info=0, operation="selfcal"
        )
    )
    skymodels = task_create_sky_model.map(
        ms=mss, sky_model_options=unmapped(sky_model_options), holofile=holofiles
    )
    addmodel_subtract_options = AddModelSubtractFieldOptions(
        wsclean_pol_mode=["i"], calibrate_container=field_options.calibrate_container
    )
    model_mss = task_addmodel_to_ms.map(
        ms=mss,
        addmodel_subtract_options=unmapped(addmodel_subtract_options),
        skymodel=skymodels,
    )
    return task_gaincal_applycal_ms.map(
        ms=model_mss,
        selfcal_round=0,
        archive_input_ms=field_options.zip_ms,
        skip_selfcal=False,
        rename_ms=field_options.rename_ms,
        archive_cal_table=True,
        casa_container=field_options.casa_container,
        update_gain_cal_options=unmapped(
            get_skymodel_gaincal_options(strategy=strategy)
        ),
    )
