from __future__ import annotations

from argparse import ArgumentParser

import pytest


def pytest_addoptions(parser: ArgumentParser):
    """Add custom flint pytesting options"""
    parser.add_argument("--skip-slow", action="store_true")


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: mark test as slow to run")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--skip-slow"):
        skip_slow = pytest.mark.skip(reason="need --runslow option to run")
        for item in items:
            if "slow" in item.keywords:
                item.add_marker(skip_slow)
