from __future__ import annotations

import filecmp
from pathlib import Path

import pytest

from flint.configuration import (
    Strategy,
    _create_mode_mapping_defaults,
    copy_and_timestamp_strategy_file,
    create_default_yaml,
    get_image_options_from_yaml,
    get_options_from_strategy,
    get_selfcal_options_from_yaml,
    load_strategy_yaml,
    verify_configuration,
    write_strategy_to_yaml,
)
from flint.utils import get_packaged_resource_path


@pytest.fixture
def package_strategy():
    example = get_packaged_resource_path(
        package="flint", filename="data/tests/test_config.yaml"
    )

    strategy = load_strategy_yaml(input_yaml=example, verify=False)

    return strategy


@pytest.fixture
def package_strategy_operations():
    example = get_packaged_resource_path(
        package="flint", filename="data/tests/test_config_2.yaml"
    )

    strategy = load_strategy_yaml(input_yaml=example, verify=False)

    return strategy


@pytest.fixture
def package_strategy_polarisation():
    example = get_packaged_resource_path(
        package="flint", filename="data/tests/test_config_pol.yaml"
    )

    strategy = load_strategy_yaml(input_yaml=example, verify=False)

    return strategy


@pytest.fixture
def strategy(tmpdir):
    output = create_default_yaml(
        output_yaml=Path(tmpdir) / "example.yaml", selfcal_rounds=3
    )

    strategy = load_strategy_yaml(input_yaml=output, verify=False)

    return strategy


@pytest.fixture
def package_strategy_path():
    example = get_packaged_resource_path(
        package="flint", filename="data/tests/test_config.yaml"
    )

    return example


def test_get_jolly_tukey_tractor_options(package_strategy_operations):
    """Ensure that we are able to access tukey tractor options"""
    strategy = package_strategy_operations
    verify_configuration(input_strategy=strategy)

    options = get_options_from_strategy(
        strategy=package_strategy_operations,
        round_info=0,
        mode="tukeytractor",
        operation="selfcal",
    )
    assert options["elevation_cut_deg"] == 10  # in the data


def test_get_potato_peel_options(package_strategy_operations):
    """Ensure that we are able to access potato peel options"""
    strategy = package_strategy_operations
    verify_configuration(input_strategy=strategy)

    options = get_options_from_strategy(
        strategy=package_strategy_operations,
        round_info=0,
        mode="potatopeel",
        operation="selfcal",
    )
    assert options["tmp"] == "JackSparrowBeHere"
    assert options["minuvpeel"] == 1112

    options = get_options_from_strategy(
        strategy=package_strategy_operations,
        mode="potatopeel",
        operation="selfcal",
    )
    assert options["tmp"] == "ThePearl"
    assert options["minuvpeel"] == 1112


def test_copy_and_timestamp(tmpdir):
    # a single function toe rename and copy a file because pirates needs to be efficient
    example = get_packaged_resource_path(
        package="flint", filename="data/tests/test_config.yaml"
    )
    copy_path = copy_and_timestamp_strategy_file(output_dir=tmpdir, input_yaml=example)

    assert copy_path != example
    assert filecmp.cmp(example, copy_path)


def test_verify_options_with_class_operations(package_strategy_operations):
    """Check whether the stokes-v and other operations around the
    verification and extraction of properties"""
    strategy = package_strategy_operations
    verify_configuration(input_strategy=strategy)

    strategy["ThisOperationDoesNotExists"] = {}
    with pytest.raises(ValueError):
        verify_configuration(input_strategy=strategy)

    strategy.pop("ThisOperationDoesNotExists")
    strategy["stokesv"]["wsclean"]["ThisDoesNotExist"] = "123"
    with pytest.raises(ValueError):
        verify_configuration(input_strategy=strategy)


def test_verify_getoptions_with_class_operations(package_strategy_operations):
    """Check whether the get options interface works with the operations
    section of the strategy file"""
    strategy = package_strategy_operations
    verify_configuration(input_strategy=strategy)

    options = get_options_from_strategy(
        strategy=package_strategy_operations, mode="wsclean", operation="stokesv"
    )
    assert options["pol"] == "V"
    assert options["channels_out"] == 2
    assert options["deconvolution_channels"] is None


def test_verify_options_with_class_missing_defaults(package_strategy):
    """Ensure that a error is raised if the defaults section is missing"""
    strategy = package_strategy
    verify_configuration(input_strategy=strategy)

    strategy.pop("defaults")
    with pytest.raises(ValueError):
        verify_configuration(input_strategy=strategy)


def test_verify_options_with_class(package_strategy):
    # ensure that the errors raised from options passed through
    # to the input structures correctly raise errors should they
    # be misconfigured (e.g. option supplied does not exist, missing
    # mandatory argument)
    strategy = package_strategy
    verify_configuration(input_strategy=strategy)

    strategy["selfcal"][0]["wsclean"]["ThisDoesNotExist"] = "ThisDoesNotExist"
    with pytest.raises(ValueError):
        verify_configuration(input_strategy=strategy)

    strategy["selfcal"][0]["wsclean"].pop("ThisDoesNotExist")
    verify_configuration(input_strategy=strategy)

    strategy["selfcal"][1]["masking"]["ThisDoesNotExist"] = "ThisDoesNotExist"

    with pytest.raises(ValueError):
        verify_configuration(input_strategy=strategy, raise_on_error=True)


def test_create_yaml_file(tmpdir):
    # ensure a default yaml strategy can be created
    output = create_default_yaml(
        output_yaml=Path(tmpdir) / "example.yaml", selfcal_rounds=3
    )

    assert output.exists()


def test_create_and_load(tmpdir):
    # ensure that a default strategy file can be both created and read back in
    output = create_default_yaml(
        output_yaml=Path(tmpdir) / "example.yaml", selfcal_rounds=3
    )

    assert output.exists()

    strategy = load_strategy_yaml(input_yaml=output)
    assert isinstance(strategy, Strategy)

    strategy = load_strategy_yaml(input_yaml=output, verify=False)
    assert isinstance(strategy, Strategy)


def test_verify(tmpdir):
    # test to make sure we can generate a default strategy (see pytest fixture)
    # read it backinto a dict and verify it is valid
    output = create_default_yaml(
        output_yaml=Path(tmpdir) / "example.yaml", selfcal_rounds=3
    )

    assert output.exists()
    strategy = load_strategy_yaml(input_yaml=output, verify=False)
    assert isinstance(strategy, Strategy)

    _ = verify_configuration(input_strategy=strategy)

    strategy["ddd"] = 123
    with pytest.raises(ValueError):
        verify_configuration(input_strategy=strategy)


def test_load_yaml_none():
    # make sure an error is raise if None is passed in. This
    # should be checked as the default value of FieldOptions.imaging_strategy
    # is None.
    with pytest.raises(TypeError):
        _ = load_strategy_yaml(input_yaml=None)  # type: ignore


def test_mode_options_mapping_creation():
    """Test that for each of the options in MODE_OPTIONS_MAPPING options can be default created"""
    defaults = _create_mode_mapping_defaults()
    assert isinstance(defaults, dict)


def test_get_options(strategy):
    # test to make sure we can generate a default strategy (see pytest fixture)
    # read it backinto a dict and then access some attributes
    wsclean = get_options_from_strategy(
        strategy=strategy, mode="wsclean", round_info=0, operation="selfcal"
    )
    assert isinstance(wsclean, dict)
    # example options
    assert wsclean["data_column"] == "CORRECTED_DATA"

    wsclean = get_options_from_strategy(
        strategy=strategy, mode="wsclean", round_info=1, operation="selfcal"
    )
    assert isinstance(wsclean, dict)
    # example options
    assert wsclean["data_column"] == "CORRECTED_DATA"

    archive = get_options_from_strategy(
        strategy=strategy, mode="archive", round_info=0, operation="selfcal"
    )
    assert isinstance(archive, dict)
    assert len(archive) > 0

    archive = get_options_from_strategy(
        strategy=strategy, mode="archive", operation="selfcal"
    )
    assert isinstance(archive, dict)
    assert len(archive) > 0


def test_get_mask_options(package_strategy):
    """Basic test to prove masking operation behaves well"""
    masking = get_options_from_strategy(
        strategy=package_strategy, mode="masking", round_info=0, operation="selfcal"
    )

    assert isinstance(masking, dict)
    assert masking["flood_fill_positive_seed_clip"] == 4.5

    masking2 = get_options_from_strategy(
        strategy=package_strategy, mode="masking", round_info=1, operation="selfcal"
    )

    print(strategy)

    assert isinstance(masking2, dict)
    assert masking2["flood_fill_positive_seed_clip"] == 40

    for k in masking.keys():
        if k == "flood_fill_positive_seed_clip":
            continue

        assert masking[k] == masking2[k]


def test_get_mask_options_from_path(package_strategy_path):
    """Basic test to prove masking operation behaves well"""
    package_strategy = Path(package_strategy_path)

    masking = get_options_from_strategy(
        strategy=package_strategy, mode="masking", round_info=0, operation="selfcal"
    )

    assert isinstance(masking, dict)
    assert masking["flood_fill_positive_seed_clip"] == 4.5

    masking2 = get_options_from_strategy(
        strategy=package_strategy, mode="masking", round_info=1, operation="selfcal"
    )

    assert isinstance(masking2, dict)
    assert masking2["flood_fill_positive_seed_clip"] == 40

    for k in masking.keys():
        if k == "flood_fill_positive_seed_clip":
            continue

        assert masking[k] == masking2[k]


def test_get_modes(package_strategy):
    # makes sure defaults for these modes are return when reuestion options
    # on a self-cal round without them set
    strategy = package_strategy

    for mode in ("wsclean", "gaincal", "masking"):
        test = get_options_from_strategy(
            strategy=strategy, mode=mode, round_info=1, operation="selfcal"
        )
        assert isinstance(test, dict)
        assert len(test.keys()) > 0


def test_bad_round(package_strategy):
    # make sure incorrect round is handled properly
    with pytest.raises(AssertionError):
        _ = get_options_from_strategy(
            strategy=package_strategy, round_info="doesnotexists", operation="selfcal"
        )

    with pytest.raises(AssertionError):
        _ = get_options_from_strategy(
            strategy=package_strategy, round_info=1.23456, operation="selfcal"
        )


def test_empty_strategy_empty_options():
    # if None is given as a strategy state then empty set of options is return
    res = get_options_from_strategy(strategy=None, operation="selfcal")

    assert isinstance(res, dict)
    assert not res == 0


def test_max_round_override(package_strategy):
    # ebsyre that the logic to switch to the highest available slefcal
    # round is sound
    strategy = package_strategy

    opts = get_options_from_strategy(
        strategy=strategy, round_info=9999, operation="selfcal"
    )
    assert isinstance(opts, dict)
    assert opts["data_column"] == "TheLastRoundIs3"


def test_empty_options(package_strategy):
    # ensures an empty options dict is return should something not set is requested
    items = get_options_from_strategy(
        strategy=package_strategy, mode="thisdoesnotexist", operation="selfcal"
    )

    assert isinstance(items, dict)
    assert len(items.keys()) == 0


def test_assert_strategy_bad():
    # tests to see if a error is raised when a bad strategy instance is passed in
    with pytest.raises(AssertionError):
        _ = get_options_from_strategy(strategy="ThisIsBad", operation="selfcal")


def test_updated_get_options(package_strategy):
    # test to make sure that the defaults outlined in a strategy file
    # are correctly overwritten should there be an options in a later
    # round. All other optiosn should remain the same.
    strategy = package_strategy
    assert isinstance(strategy, Strategy)

    wsclean_init = get_options_from_strategy(
        strategy=strategy, mode="wsclean", round_info=0, operation="selfcal"
    )
    assert wsclean_init["data_column"] == "CORRECTED_DATA"

    wsclean_1 = get_options_from_strategy(
        strategy=strategy, mode="wsclean", round_info=1, operation="selfcal"
    )
    assert wsclean_init["data_column"] == "CORRECTED_DATA"
    assert wsclean_1["data_column"] == "EXAMPLE"

    wsclean_2 = get_options_from_strategy(
        strategy=strategy, mode="wsclean", round_info=2, operation="selfcal"
    )
    assert wsclean_2["multiscale"] is False
    assert wsclean_2["data_column"] == "CORRECTED_DATA"

    assert all(
        [
            wsclean_init[key] == wsclean_1[key]
            for key in wsclean_init.keys()
            if key != "data_column"
        ]
    )
    assert wsclean_init["data_column"] != wsclean_1["data_column"]

    assert all(
        [
            wsclean_init[key] == wsclean_2[key]
            for key in wsclean_init.keys()
            if key != "multiscale"
        ]
    )


def test_get_image_options():
    init = get_image_options_from_yaml(input_yaml=None, self_cal_rounds=False)

    assert isinstance(init, dict)

    rounds = get_image_options_from_yaml(input_yaml=None, self_cal_rounds=True)

    assert isinstance(init, dict)
    assert 1 in rounds.keys()


def test_raise_image_options_error():
    example = Path("example.yaml")

    with pytest.raises(AssertionError):
        get_image_options_from_yaml(input_yaml=example)


def test_self_cal_options():
    rounds = get_selfcal_options_from_yaml(input_yaml=None)

    assert isinstance(rounds, dict)
    assert 1 in rounds.keys()


def test_raise_error_options_error():
    example = Path("example.yaml")

    with pytest.raises(AssertionError):
        get_selfcal_options_from_yaml(input_yaml=example)


def test_write_strategy_to_yaml(package_strategy, tmpdir):
    strategy = package_strategy

    output_strategy = Path(tmpdir) / "testing.yaml"
    write_strategy_to_yaml(strategy=strategy, output_path=output_strategy)

    loaded_strategy = load_strategy_yaml(input_yaml=output_strategy)

    assert len(set(loaded_strategy.keys()) - set(strategy.keys())) == 0
    assert (
        len(
            set(loaded_strategy["selfcal"][0].keys())
            - set(strategy["selfcal"][0].keys())
        )
        == 0
    )
    assert (
        len(
            set(loaded_strategy["selfcal"][1].keys())
            - set(strategy["selfcal"][1].keys())
        )
        == 0
    )
    assert (
        loaded_strategy["selfcal"][1]["wsclean"]["data_column"]
        == strategy["selfcal"][1]["wsclean"]["data_column"]
    )


def test_polarisation_options(package_strategy_polarisation):
    strategy = package_strategy_polarisation
    verify_configuration(input_strategy=strategy)

    total_options = get_options_from_strategy(
        strategy=strategy,
        operation="polarisation",
        polarisation="total",
    )
    assert total_options["pol"] == "i"
    assert not total_options["squared_channel_joining"]

    linear_options = get_options_from_strategy(
        strategy=strategy,
        operation="polarisation",
        polarisation="linear",
    )
    assert linear_options["pol"] == "qu"
    assert linear_options["squared_channel_joining"]
    assert linear_options["join_polarizations"]

    circular_options = get_options_from_strategy(
        strategy=strategy,
        operation="polarisation",
        polarisation="circular",
    )
    assert circular_options["pol"] == "v"
