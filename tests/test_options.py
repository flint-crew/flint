"""Very basic tests to make sure the FieldOptions class is
somewhat tracked, especially when using an argparse object
to create it
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic.fields import FieldInfo

from flint.options import (
    FieldOptions,
    _create_argparse_options,
    create_options_from_parser,
    dump_field_options_to_yaml,
    options_to_dict,
)
from flint.prefect.flows.continuum_pipeline import get_parser


def test_fieldinfo_to_argparse_options():
    """The pydantic ``FieldInfo`` object is used to generate the options that would be
    splat into an ArgumentParser.add_argument method. Ensure the expected mappings from
    types to argument options make sense"""
    field = FieldInfo(default=1, annotation=int, description="An example description")
    field_name, field_options = _create_argparse_options(
        name="jack_sparrow", field=field
    )
    assert field_name == "--jack-sparrow"
    assert field_options["action"] == "store"
    assert field_options["default"] == 1
    assert field_options["help"] == "An example description"

    field = FieldInfo(annotation=int, description="An example description")
    field_name, field_options = _create_argparse_options(
        name="jack_sparrow", field=field
    )
    assert field_name == "jack_sparrow"
    assert field_options["action"] == "store"
    assert field_options["help"] == "An example description"

    field = FieldInfo(
        default=[1, 2, 3, 4], annotation=list[int], description="An example description"
    )
    field_name, field_options = _create_argparse_options(
        name="jack_sparrow", field=field
    )
    assert field_name == "--jack-sparrow"
    assert field_options["action"] == "store"
    assert field_options["default"] == [1, 2, 3, 4]
    assert field_options["help"] == "An example description"
    assert field_options["nargs"] == "+"

    field = FieldInfo(
        default=("foo", "bar", 3),
        annotation=tuple[str, str, int],
    )
    field_name, field_options = _create_argparse_options(
        name="jack_sparrow", field=field
    )
    assert field_options["default"] == ("foo", "bar", 3)
    assert field_options["nargs"] == 3

    field = FieldInfo(
        default=("foo", "bar"),
        annotation=tuple[str, ...],
    )
    field_name, field_options = _create_argparse_options(
        name="jack_sparrow", field=field
    )
    assert field_options["default"] == (
        "foo",
        "bar",
    )
    assert field_options["nargs"] == "+"

    field = FieldInfo(
        default=None,
        annotation=tuple[str, str] | None,
    )
    field_name, field_options = _create_argparse_options(
        name="jack_sparrow", field=field
    )
    assert field_options["default"] == None  # noqa E711
    assert field_options["nargs"] == 2


def test_options_to_dict():
    """See ifthe utility around converting Option/Results to dictionary works"""
    flagger_container = Path("a")
    calibrate_container = Path("b")
    field_options_1 = FieldOptions(
        flagger_container=flagger_container, calibrate_container=calibrate_container
    )
    field_options_2 = dict(
        flagger_container=flagger_container, calibrate_container=calibrate_container
    )

    for field_options in (field_options_1, field_options_2):
        field_dict = options_to_dict(input_options=field_options)
        assert isinstance(field_dict, dict)
        assert field_dict["flagger_container"] == flagger_container
        assert field_dict["calibrate_container"] == calibrate_container

    with pytest.raises(TypeError):
        _ = options_to_dict(input_options="Jack")


def test_dump_field_options_to_yaml(tmpdir):
    """See if the field options file can be dumped to an output directory"""
    tmpdir = Path(tmpdir)

    field_options = FieldOptions(
        flagger_container=Path("a"), calibrate_container=Path("b")
    )

    assert not (tmpdir / "Jack").exists()

    path_1 = tmpdir / "field_options.yaml"
    path_2 = tmpdir / "Jack" / "Sparrow" / "field_options.yaml"

    for path in (path_1, path_2):
        output_path = dump_field_options_to_yaml(
            output_path=path, field_options=field_options
        )
        assert output_path.exists()

    with pytest.raises(FileExistsError):
        dump_field_options_to_yaml(output_path=path_2, field_options=field_options)


def test_config_field_options(tmpdir):
    output_file = f"{tmpdir}/example.config"
    contents = """--holofile /scratch3/projects/spiceracs/RACS_Low2_Holography/akpb.iquv.square_6x6.63.887MHz.SB39549.cube.fits
        --calibrate-container /scratch3/gal16b/containers/calibrate.sif
        --flagger-container /scratch3/gal16b/containers/aoflagger.sif
        --wsclean-container /scratch3/projects/spiceracs/singularity_images/wsclean_force_mask.sif
        --yandasoft-container /scratch3/gal16b/containers/yandasoft.sif
        --cluster-config /scratch3/gal16b/split/petrichor.yaml
        --rounds 2
        --split-path $(pwd)
        --zip-ms
        --run-aegean
        --use-beam-masks
        --use-preflagger
        --aegean-container '/scratch3/gal16b/containers/aegean.sif'
        --reference-catalogue-directory '/scratch3/gal16b/reference_catalogues/'
        --linmos-residuals
    """

    with open(output_file, "w") as out:
        for line in contents.split("\n"):
            out.write(f"{line.lstrip()}\n")

    parser = get_parser()
    args = parser.parse_args(
        f"""/scratch3/gal16b/askap_sbids/112334/
        --calibrated-bandpass-path /scratch3/gal16b/askap_sbids/111/
        --cli-config {output_file!s}""".split()
    )

    field_options = create_options_from_parser(
        parser_namespace=args, options_class=FieldOptions
    )

    assert isinstance(field_options, FieldOptions)
    assert field_options.zip_ms is True
    assert field_options.linmos_residuals is True
    assert field_options.rounds == 2
    assert isinstance(field_options.wsclean_container, Path)
    assert field_options.use_beam_masks
    assert field_options.use_preflagger


def test_create_field_options():
    parser = get_parser()
    args = parser.parse_args(
        """/scratch3/gal16b/askap_sbids/112334/
        --calibrated-bandpass-path /scratch3/gal16b/askap_sbids/111/
        --holofile /scratch3/projects/spiceracs/RACS_Low2_Holography/akpb.iquv.square_6x6.63.887MHz.SB39549.cube.fits
        --calibrate-container /scratch3/gal16b/containers/calibrate.sif
        --flagger-container /scratch3/gal16b/containers/aoflagger.sif
        --wsclean-container /scratch3/projects/spiceracs/singularity_images/wsclean_force_mask.sif
        --yandasoft-container /scratch3/gal16b/containers/yandasoft.sif
        --cluster-config /scratch3/gal16b/split/petrichor.yaml
        --rounds 2
        --split-path $(pwd)
        --zip-ms
        --run-aegean
        --aegean-container '/scratch3/gal16b/containers/aegean.sif'
        --reference-catalogue-directory '/scratch3/gal16b/reference_catalogues/'
        --linmos-residuals
    """.split()
    )

    field_options = FieldOptions(
        flagger_container=args.flagger_container,
        calibrate_container=args.calibrate_container,
        holofile=args.holofile,
        expected_ms=args.expected_ms,
        wsclean_container=args.wsclean_container,
        yandasoft_container=args.yandasoft_container,
        rounds=args.rounds,
        zip_ms=args.zip_ms,
        run_aegean=args.run_aegean,
        aegean_container=args.aegean_container,
        no_imaging=args.no_imaging,
        reference_catalogue_directory=args.reference_catalogue_directory,
        linmos_residuals=args.linmos_residuals,
        beam_cutoff=args.beam_cutoff,
        pb_cutoff=args.pb_cutoff,
        use_preflagger=args.use_preflagger,
    )

    assert isinstance(field_options, FieldOptions)
    assert field_options.use_preflagger is False
    assert field_options.zip_ms is True
    assert field_options.linmos_residuals is True
    assert field_options.rounds == 2
    assert isinstance(field_options.wsclean_container, Path)


def test_create_field_options2():
    parser = get_parser()
    args = parser.parse_args(
        """/scratch3/gal16b/askap_sbids/112334/
        --calibrated-bandpass-path /scratch3/gal16b/askap_sbids/111/
        --holofile /scratch3/projects/spiceracs/RACS_Low2_Holography/akpb.iquv.square_6x6.63.887MHz.SB39549.cube.fits
        --calibrate-container /scratch3/gal16b/containers/calibrate.sif
        --flagger-container /scratch3/gal16b/containers/aoflagger.sif
        --wsclean-container /scratch3/projects/spiceracs/singularity_images/wsclean_force_mask.sif
        --yandasoft-container /scratch3/gal16b/containers/yandasoft.sif
        --cluster-config /scratch3/gal16b/split/petrichor.yaml
        --rounds 2
        --split-path $(pwd)
        --run-aegean
        --aegean-container '/scratch3/gal16b/containers/aegean.sif'
        --reference-catalogue-directory '/scratch3/gal16b/reference_catalogues/'
        --use-preflagger
    """.split()
    )

    field_options = FieldOptions(
        flagger_container=args.flagger_container,
        calibrate_container=args.calibrate_container,
        holofile=args.holofile,
        expected_ms=args.expected_ms,
        wsclean_container=args.wsclean_container,
        yandasoft_container=args.yandasoft_container,
        rounds=args.rounds,
        zip_ms=args.zip_ms,
        run_aegean=args.run_aegean,
        aegean_container=args.aegean_container,
        no_imaging=args.no_imaging,
        reference_catalogue_directory=args.reference_catalogue_directory,
        linmos_residuals=args.linmos_residuals,
        beam_cutoff=args.beam_cutoff,
        pb_cutoff=args.pb_cutoff,
        use_preflagger=args.use_preflagger,
    )

    assert isinstance(field_options, FieldOptions)
    assert field_options.use_preflagger is True
    assert field_options.zip_ms is False
    assert field_options.linmos_residuals is False
    assert field_options.rounds == 2
    assert isinstance(field_options.wsclean_container, Path)


def test_create_field_options3():
    """Make sure that the calibrated-bandpass-path can be left unchecked"""
    parser = get_parser()
    args = parser.parse_args(
        """/scratch3/gal16b/askap_sbids/112334/
        --holofile /scratch3/projects/spiceracs/RACS_Low2_Holography/akpb.iquv.square_6x6.63.887MHz.SB39549.cube.fits
        --calibrate-container /scratch3/gal16b/containers/calibrate.sif
        --flagger-container /scratch3/gal16b/containers/aoflagger.sif
        --wsclean-container /scratch3/projects/spiceracs/singularity_images/wsclean_force_mask.sif
        --yandasoft-container /scratch3/gal16b/containers/yandasoft.sif
        --cluster-config /scratch3/gal16b/split/petrichor.yaml
        --rounds 2
        --split-path $(pwd)
        --run-aegean
        --aegean-container '/scratch3/gal16b/containers/aegean.sif'
        --reference-catalogue-directory '/scratch3/gal16b/reference_catalogues/'
        --use-preflagger
    """.split()
    )

    field_options = FieldOptions(
        flagger_container=args.flagger_container,
        calibrate_container=args.calibrate_container,
        holofile=args.holofile,
        expected_ms=args.expected_ms,
        wsclean_container=args.wsclean_container,
        yandasoft_container=args.yandasoft_container,
        rounds=args.rounds,
        zip_ms=args.zip_ms,
        run_aegean=args.run_aegean,
        aegean_container=args.aegean_container,
        no_imaging=args.no_imaging,
        reference_catalogue_directory=args.reference_catalogue_directory,
        linmos_residuals=args.linmos_residuals,
        beam_cutoff=args.beam_cutoff,
        pb_cutoff=args.pb_cutoff,
        use_preflagger=args.use_preflagger,
    )

    assert isinstance(field_options, FieldOptions)
    assert field_options.use_preflagger is True
    assert field_options.zip_ms is False
    assert field_options.linmos_residuals is False
    assert field_options.rounds == 2
    assert isinstance(field_options.wsclean_container, Path)
    assert args.calibrated_bandpass_path is None
