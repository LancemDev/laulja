from __future__ import annotations

import pyfiglet
from rich.console import Console
from rich.text import Text

"""Styled terminal output for everything laulja prints before (or instead of) the TUI — the
setup/sign-in flow, `--login`, and the startup screen — in the Monokai palette."""

PINK = "#F92672"
ORANGE = "#FD971F"
YELLOW = "#E6DB74"
GREEN = "#A6E22E"
BLUE = "#66D9EF"
PURPLE = "#AE81FF"
COMMENT = "#75715E"
FOREGROUND = "#F8F8F2"

_GRADIENT = [PINK, ORANGE, YELLOW, GREEN, BLUE, PURPLE]

console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _gradient_at(ratio: float) -> str:
    """Color at `ratio` (0–1) along the Monokai gradient, linearly blended between stops."""
    scaled = max(0.0, min(1.0, ratio)) * (len(_GRADIENT) - 1)
    i = min(int(scaled), len(_GRADIENT) - 2)
    t = scaled - i
    a, b = _hex_to_rgb(_GRADIENT[i]), _hex_to_rgb(_GRADIENT[i + 1])
    r, g, bl = (round(x + (y - x) * t) for x, y in zip(a, b))
    return f"#{r:02x}{g:02x}{bl:02x}"


def banner(subtitle: str | None = None) -> None:
    """Big block-letter "laulja" wordmark, colored column-by-column along the gradient."""
    art = [line for line in pyfiglet.figlet_format("laulja", font="ansi_shadow").split("\n") if line.strip()]
    width = max(len(line) for line in art)
    if console.width < width + 2:
        # Too narrow for the block letters — fall back to a single gradient line.
        art, width = ["laulja"], 6

    console.print()
    for line in art:
        text = Text("  ")
        for col, char in enumerate(line):
            text.append(char, style=f"bold {_gradient_at(col / max(width - 1, 1))}")
        console.print(text)
    tagline = Text("  YouTube Music, in your terminal", style=f"italic {COMMENT}")
    if subtitle:
        tagline.append("  ·  ", style=COMMENT)
        tagline.append(subtitle, style=f"bold {PINK}")
    console.print(tagline)
    console.print()


def step(number: int, total: int, message: str) -> None:
    console.print(
        Text.assemble(
            ("  ", ""),
            (f" {number}/{total} ", f"bold #272822 on {PINK}"),
            ("  ", ""),
            (message, f"bold {FOREGROUND}"),
        )
    )


def info(message: str) -> None:
    console.print(Text.assemble(("  › ", f"bold {BLUE}"), (message, FOREGROUND)))


def hint(message: str) -> None:
    console.print(Text("    " + message, style=COMMENT))


def success(message: str) -> None:
    console.print(Text.assemble(("  ✔ ", f"bold {GREEN}"), (message, f"bold {GREEN}")))


def warn(message: str) -> None:
    console.print(Text.assemble(("  ! ", f"bold {ORANGE}"), (message, ORANGE)))


def error(message: str) -> None:
    err_console.print(Text.assemble(("  ✘ ", f"bold {PINK}"), (message, PINK)))


def key_value(key: str, value: str) -> None:
    console.print(Text.assemble((f"  {key:<10}", f"bold {PURPLE}"), (value, FOREGROUND)))


def prompt_text(message: str) -> str:
    """ANSI-styled prompt string for input() — input() itself doesn't go through rich."""
    with console.capture() as capture:
        console.print(Text.assemble(("  ? ", f"bold {YELLOW}"), (message, f"bold {YELLOW}")), end="")
    return capture.get()


def spinner_frame(frame: str, message: str) -> str:
    with console.capture() as capture:
        console.print(Text.assemble((f"  {frame} ", f"bold {PINK}"), (message, FOREGROUND)), end="")
    return capture.get()
