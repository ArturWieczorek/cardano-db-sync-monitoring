"""Shared, role-agnostic machinery for assembling stats reports.

A "report" is a per-environment document built from the time-series plots the
`*-plot.py` scripts produce (the figures are passed in already built). Two
renderings are supported:

  - **PNG + Markdown**: each figure is rendered to a static PNG (needs the
    optional `kaleido` package) and referenced from a Markdown file - the
    paste-into-a-wiki workflow that previously meant clicking Plotly's camera
    button by hand.
  - **Self-contained HTML**: the interactive figures are embedded in one HTML
    file (Plotly's JS is bundled once); no extra dependency.

cardano-db-sync uses this first; cardano-node will reuse it.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from pathlib import Path

from plotly.graph_objs import Figure

# Default static-image geometry. Responsive figures (e.g. cpu_ram) carry no
# layout height, so PNG export - which needs explicit pixels - falls back to
# this; figures that pin their own height (ingest/tables) keep it.
PNG_WIDTH = 1500
PNG_FALLBACK_HEIGHT = 760
PNG_SCALE = 2

# Height (px) given to a responsive (no-fixed-height) figure when it is embedded
# in the multi-figure report HTML. A height-less figure renders as height:100%,
# which fills the viewport in a standalone page but collapses to nothing when
# stacked inside the report body - so we pin one. Generous on purpose: these are
# the few-panel headline charts (cpu_ram is 2 panels -> ~450px each), so they get
# more room than the dense per-metric plots. Independent of the PNG height.
HTML_FALLBACK_HEIGHT = 900


class KaleidoMissingError(RuntimeError):
    """Raised when PNG export is requested but the `kaleido` backend is absent."""


def render_png(
    fig: Figure, path: str | Path, *, width: int = PNG_WIDTH, height: int | None = None, scale: int = PNG_SCALE
) -> None:
    """Write `fig` to `path` as PNG. Height defaults to the figure's own layout
    height, else `PNG_FALLBACK_HEIGHT`. Raises `KaleidoMissingError` with an
    actionable hint when the kaleido backend isn't installed."""
    h = height or fig.layout.height or PNG_FALLBACK_HEIGHT
    try:
        fig.write_image(str(path), format="png", width=width, height=int(h), scale=scale)
    except Exception as e:
        if _looks_like_missing_kaleido(e):
            raise KaleidoMissingError(
                "PNG export needs the 'kaleido' package, which isn't installed. "
                "Install the report extra:  pip install '.[report]'  (or: "
                "pip install kaleido). HTML output (--format html) needs nothing extra."
            ) from e
        raise


def _looks_like_missing_kaleido(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "kaleido" in msg or isinstance(exc, ModuleNotFoundError)


def downsample_figure(fig: Figure, max_points: int) -> Figure:
    """Return a copy of `fig` with each trace's points uniformly strided down to
    at most `max_points`, to keep the interactive HTML embed light. A
    multi-day, 10s-cadence series can carry 100k+ points per trace - far more
    than a ~1500px-wide screen can resolve - so striding to a few thousand is
    visually identical for the smooth/cumulative curves these reports show. The
    original figure is untouched (static PNGs are rendered at full resolution).

    `max_points <= 0` is a no-op (full fidelity).
    """
    if max_points <= 0:
        return fig
    out = copy.deepcopy(fig)
    for tr in out.data:
        x = getattr(tr, "x", None)
        if x is None or len(x) <= max_points:
            continue
        step = math.ceil(len(x) / max_points)
        # Materialize to a list first: a trace's x/y may be a tuple, ndarray, or
        # pandas object, and not all slice uniformly - list() + stride always does.
        tr.x = list(x)[::step]
        y = getattr(tr, "y", None)
        if y is not None:
            tr.y = list(y)[::step]
    return out


def fig_to_html_fragment(fig: Figure, *, include_plotlyjs: bool | str = False) -> str:
    """Figure as an embeddable HTML `<div>`. By default the Plotly JS is NOT
    inlined (the assembling document includes it once); pass `"cdn"`/True for a
    standalone fragment."""
    return fig.to_html(full_html=False, include_plotlyjs=include_plotlyjs)


@dataclass
class ReportImage:
    """One rendered figure within a section: its caption, the on-disk PNG file
    name (Markdown) and the built figure (HTML embed)."""

    caption: str
    png_name: str
    fig: Figure


@dataclass
class ReportSection:
    """A titled group of images (e.g. the InMemory build, or a comparison)."""

    title: str
    images: list[ReportImage] = field(default_factory=list)
    level: int = 2  # markdown heading depth


def assemble_markdown(title: str, sections: list[ReportSection], *, subtitle: str = "") -> str:
    """Render the report as Markdown, referencing each image by its PNG name
    (relative - PNGs are written next to the .md)."""
    lines: list[str] = [f"# {title}", ""]
    if subtitle:
        lines += [subtitle, ""]
    for sec in sections:
        lines += [f"{'#' * sec.level} {sec.title}", ""]
        for img in sec.images:
            lines += [f"**{img.caption}**", "", f"![{img.caption}]({img.png_name})", ""]
    return "\n".join(lines).rstrip() + "\n"


def assemble_html(
    title: str, sections: list[ReportSection], *, subtitle: str = "", max_points: int | None = None
) -> str:
    """Render the report as one self-contained HTML page with every figure
    embedded interactively. Plotly's JS is inlined once (in the first fragment)
    and reused for the rest, so the file is fully offline-standalone without
    bundling the library N times.

    `max_points` (per trace) downsamples the embedded data to keep the file
    small; None/0 embeds full-resolution data (default)."""
    parts: list[str] = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        f"<title>{title}</title>",
        "<style>body{font-family:system-ui,sans-serif;max-width:1600px;margin:0 auto;"
        "padding:1rem} h2{border-bottom:2px solid #ddd;padding-top:1rem} "
        ".cap{font-weight:600;margin:.5rem 0}</style>",
        "</head><body>",
        f"<h1>{title}</h1>",
    ]
    if subtitle:
        parts.append(f"<p>{subtitle}</p>")
    first = True
    for sec in sections:
        parts.append(f"<h{sec.level}>{sec.title}</h{sec.level}>")
        for img in sec.images:
            parts.append(f"<div class='cap'>{img.caption}</div>")
            fig = downsample_figure(img.fig, max_points) if max_points else img.fig
            # A responsive (height-less) figure renders as height:100%, which
            # collapses when stacked in the report body - pin a height so it
            # doesn't squish. Copy first if we haven't already, to avoid mutating
            # the caller's figure (it may also be used for the PNG).
            if fig.layout.height is None:
                if fig is img.fig:
                    fig = copy.deepcopy(fig)
                fig.layout.height = HTML_FALLBACK_HEIGHT
            # Inline the full plotly.js once (first fragment) so the file is
            # offline-standalone; reuse it for the rest.
            parts.append(fig_to_html_fragment(fig, include_plotlyjs=(True if first else False)))
            first = False
    parts.append("</body></html>")
    return "\n".join(parts)
