"""Prefect tasks around model visibility prediction"""

from __future__ import annotations

from pathlib import Path

from flint.logging import logger
from flint.ms import MS
from flint.options import AddModelSubtractFieldOptions
from flint.predict.crystalball import CrystalBallOptions
from flint.prefect.caching import task
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
    assert skymodel is not None, (
        f"No sky-model sources survived the flux/radial cuts for {ms.path}"
    )

    return skymodel
