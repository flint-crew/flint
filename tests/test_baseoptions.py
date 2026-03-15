"""Some specific tests around the pydantic base options model
that we are using to construct a BaseOptions class"""

from __future__ import annotations

from argparse import ArgumentParser

import pytest
from capn_crunch import BaseOptions, add_options_to_parser, create_options_from_parser
from pydantic import ValidationError


def test_ensure_options_frozen():
    """BaseOption classes should be immutable, so an error
    should be raised"""

    class NewOptions(BaseOptions):
        a: int
        """An example"""
        b: float
        """Another example"""

    new_options = NewOptions(a=1, b=1.23)
    with pytest.raises(ValidationError):
        # can't update the immutable class
        new_options.a = 33
        # raise error on argument not existing
        _ = NewOptions(a=1, b=1, jack="sparrow")  # type: ignore


def test_baseoptions_argparse():
    """Create an argument parser from a BaseOptions"""

    class NewOptions(BaseOptions):
        a: int
        """An example"""
        b: float
        """Another example"""
        c: bool = False
        """A flag"""

    parser = ArgumentParser(description="Jack Sparrow")

    parser = add_options_to_parser(parser=parser, options_class=NewOptions)
    args = parser.parse_args("1 1.23 --c".split())
    assert args.a == "1"
    assert isinstance(args.a, str)
    assert args.b == "1.23"
    assert isinstance(args.b, str)
    assert args.c
    assert isinstance(args.c, bool)

    new_options = create_options_from_parser(
        parser_namespace=args, options_class=NewOptions
    )
    assert isinstance(new_options, NewOptions)
    assert new_options.a == 1
    assert isinstance(new_options.a, int)
    assert new_options.b == 1.23
    assert isinstance(new_options.b, float)


def test_create_new_options():
    """Create a new subclass of BaseOptions"""

    class NewOptions(BaseOptions):
        a: int
        """An example"""
        b: float
        """Another example"""

    new_options = NewOptions(a=1, b=1.23)
    assert new_options.a == 1
    assert new_options.b == 1.23

    update_options = new_options.with_options(b=234.3)
    assert update_options.b == 234.3

    assert new_options is not update_options

    # Make sure the types are properly cast
    new_options = NewOptions(a=1, b=1)
    assert isinstance(new_options.b, float)


def test_create_new_options_asdict():
    """Create a new subclass of BaseOptions"""

    class NewOptions(BaseOptions):
        a: int
        """An example"""
        b: float
        """Another example"""

    new_options = NewOptions(a=1, b=1.23)
    _dict = new_options._asdict()
    assert isinstance(_dict, dict)
    assert _dict["a"] == 1
    assert isinstance(_dict["a"], int)
