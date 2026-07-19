"""Sphinx configuration for the zpf documentation."""

from __future__ import annotations

project = "zpf"
author = "Adam Jonsson"
project_copyright = "2026, Adam Jonsson"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "myst_parser",
]

myst_heading_anchors = 3

exclude_patterns = ["_build"]

html_theme = "furo"
