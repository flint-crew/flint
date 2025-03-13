"""Tests around the sky-model code"""

from pathlib import Path
import pytest

from flint.options import create_options_from_parser
from flint.sky_model import (
    get_parser, 
    SkyModelOptions, 
    SkyModelOutputPaths, 
    get_sky_model_output_paths
)

def test_get_working_parser():
    """Make sure that the interaction with the SkyModelOptions 
    and the argument parser works"""
    parser = get_parser()
    
    args = parser.parse_args("example.ms".split())
    ms = Path(args.ms)
    assert ms == Path("example.ms")
    
    sky_model_options = create_options_from_parser(parser_namespace=args, options_class=SkyModelOptions)    
    assert isinstance(sky_model_options, SkyModelOptions)
    assert isinstance(sky_model_options.reference_catalogue_directory, Path)
    
    
def test_get_sky_model_output_names():
    """Ensure the names are what we expect them to be"""
    
    ms_path = Path("JackSparrowData.ms")
    
    sky_model_outpout_paths = get_sky_model_output_paths(ms_path=ms_path)
    assert isinstance(sky_model_outpout_paths, SkyModelOutputPaths)
    assert sky_model_outpout_paths.hyperdrive_path == Path("JackSparrowData.hyperdrive.yaml")
    assert sky_model_outpout_paths.calibrate_path == Path("JackSparrowData.calibrate.txt")
    
    with pytest.raises(ValueError):
        get_sky_model_output_paths(ms_path=Path("JackBeNoMS.txt"))
        