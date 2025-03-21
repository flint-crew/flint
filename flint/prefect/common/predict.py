"""Prefect tasks around model visibility prediction"""

from __future__ import annotations

from prefect import task

from flint.logging import logger
from flint.ms import MS
from flint.options import AddModelSubtractFieldOptions


@task
def task_addmodel_to_ms(
    ms: MS,
    addmodel_subtract_options: AddModelSubtractFieldOptions,
) -> MS:
    from flint.imager.wsclean import get_wsclean_output_source_list_path
    from flint.predict.addmodel import AddModelOptions, add_model

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
        assert addmodel_subtract_options.calibrate_container is not None, (
            f"{addmodel_subtract_options.calibrate_container=}, which should not happen"
        )
        add_model(
            add_model_options=addmodel_options,
            container=addmodel_subtract_options.calibrate_container,
            remove_datacolumn=idx == 0,
        )

    return ms.with_options(model_column="MODEL_DATA")
