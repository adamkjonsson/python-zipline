"""Sphinx configuration for the zpf documentation."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from docutils.nodes import Element, TextElement
    from sphinx.addnodes import pending_xref
    from sphinx.application import Sphinx
    from sphinx.environment import BuildEnvironment

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

# Report every cross-reference that does not resolve, so a broken one fails
# the ``-W`` build instead of silently rendering as plain text. The exceptions
# below are the references that cannot resolve here and are not defects:
#
# * standard-library names — linking them needs intersphinx, and that would
#   make every docs build depend on the network;
# * private aliases used in otherwise public signatures;
# * ``tuple[X, ...]`` annotations, which Sphinx's type parser splits on the
#   comma and then fails to look up as one name.
nitpicky = True

nitpick_ignore = [
    ("py:class", "collections.abc.Callable"),
    ("py:class", "collections.abc.Mapping"),
    ("py:class", "datetime"),
    ("py:class", "datetime.datetime"),
    ("py:class", "os.PathLike"),
    ("py:func", "dataclasses.replace"),
    ("py:mod", "hashlib"),
    ("py:class", "_Cites"),
    ("py:class", "_SessionIndex"),
]

nitpick_ignore_regex = [("py:class", r"tuple\[.*")]


def _reexport_aliases() -> dict[str, str]:
    """Map each top-level re-export to the module that actually defines it."""
    import zpf

    aliases: dict[str, str] = {}
    for name in zpf.__all__:
        obj = getattr(zpf, name)
        module = getattr(obj, "__module__", None)
        if module is not None and module.startswith("zpf."):
            aliases[f"zpf.{name}"] = f"{module}.{name}"
    return aliases


_ALIASES = _reexport_aliases()


def _resolve_reexport(
    app: Sphinx,
    env: BuildEnvironment,
    node: pending_xref,
    contnode: TextElement,
) -> Element | None:
    """Resolve ``zpf.Foo`` references against ``zpf.<module>.Foo``.

    The prose cites the top-level re-export, because ``import zpf`` is all a
    consumer needs, while ``api/*`` documents each symbol under the module
    that defines it. Without this bridge every such reference — including
    members like ``zpf.FileWriter.derive_from`` — silently renders as an
    unlinked literal instead of a broken-reference warning.

    Args:
        app: The running Sphinx application.
        env: The build environment.
        node: The cross-reference that failed to resolve.
        contnode: The node holding the reference's display text.

    Returns:
        The resolved reference node, or ``None`` to leave the reference
        unresolved (so ``nitpicky`` still reports a genuine typo).

    """
    if node.get("refdomain") != "py":
        return None
    parts = node.get("reftarget", "").split(".")
    if len(parts) < 2 or parts[0] != "zpf":
        return None
    head = _ALIASES.get(f"zpf.{parts[1]}")
    if head is None:
        return None
    canonical = ".".join([head, *parts[2:]])
    domain = env.domains["py"]
    if canonical not in domain.objects:
        return None
    return domain.resolve_xref(
        env, node["refdoc"], app.builder, node["reftype"], canonical, node, contnode
    )


def setup(app: Sphinx) -> None:
    """Register the documentation's build-time hooks."""
    app.connect("missing-reference", _resolve_reexport)
