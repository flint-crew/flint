# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information
from __future__ import annotations

import shutil
from pathlib import Path

import flint

project = "flint"
copyright = "2025, Tim Galvin"
author = "Tim Galvin"
version = release = flint.__version__

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration


extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx_autodoc_typehints",
    "sphinx.ext.doctest",
    "sphinx.ext.intersphinx",
    "sphinx.ext.todo",
    "sphinx.ext.coverage",
    "sphinx.ext.mathjax",
    "sphinx.ext.ifconfig",
    "sphinx.ext.viewcode",
    "sphinx.ext.githubpages",
    "sphinx.ext.napoleon",
    "sphinx_copybutton",
    "autoapi.extension",
    "sphinxarg.ext",
]

myst_enable_extensions = [
    "colon_fence",
]

autoapi_type = "python"
autoapi_dirs = ["../flint"]
autoapi_member_order = "groupwise"
autoapi_keep_files = False
autoapi_root = "autoapi"
autoapi_add_toctree_entry = True

# Napoleon settings
napoleon_google_docstring = True
napoleon_numpy_docstring = True
napoleon_include_init_with_doc = True
napoleon_include_private_with_doc = True
napoleon_include_special_with_doc = True
napoleon_use_admonition_for_examples = False
napoleon_use_admonition_for_notes = False
napoleon_use_admonition_for_references = False
napoleon_use_ivar = False
napoleon_use_param = True
napoleon_use_rtype = True

source_suffix = [".rst", ".md"]
templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]


# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = "furo"
# html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]

# copy logo to _static
static_path = Path("_static")
logo_path = Path("../_static/logo.jpeg")

shutil.copyfile(logo_path, static_path / logo_path.name)
