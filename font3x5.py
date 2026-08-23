"""A tiny 3x5 pixel font.

The Sense HAT library's own show_message() blocks until the whole string has
scrolled, which makes it impossible to interrupt.  This module renders text to
a list of columns instead, so the caller can step through it one column at a
time and check a cancellation token between steps.

render_text() returns a list of columns.  Each column is a list of 8 booleans,
indexed by row (0 = top), matching the Sense HAT's 8x8 matrix.
"""

from __future__ import annotations

GLYPHS: dict[str, tuple[str, ...]] = {
    "A": (".#.", "#.#", "###", "#.#", "#.#"),
    "B": ("##.", "#.#", "##.", "#.#", "##."),
    "C": (".##", "#..", "#..", "#..", ".##"),
    "D": ("##.", "#.#", "#.#", "#.#", "##."),
    "E": ("###", "#..", "##.", "#..", "###"),
    "F": ("###", "#..", "##.", "#..", "#.."),
    "G": (".##", "#..", "#.#", "#.#", ".##"),
    "H": ("#.#", "#.#", "###", "#.#", "#.#"),
    "I": ("###", ".#.", ".#.", ".#.", "###"),
    "J": ("..#", "..#", "..#", "#.#", ".#."),
    "K": ("#.#", "#.#", "##.", "#.#", "#.#"),
    "L": ("#..", "#..", "#..", "#..", "###"),
    "M": ("#.#", "###", "###", "#.#", "#.#"),
    "N": ("#.#", "##.", "###", ".##", "#.#"),
    "O": (".#.", "#.#", "#.#", "#.#", ".#."),
    "P": ("##.", "#.#", "##.", "#..", "#.."),
    "Q": (".#.", "#.#", "#.#", "##.", ".##"),
    "R": ("##.", "#.#", "##.", "#.#", "#.#"),
    "S": (".##", "#..", ".#.", "..#", "##."),
    "T": ("###", ".#.", ".#.", ".#.", ".#."),
    "U": ("#.#", "#.#", "#.#", "#.#", "###"),
    "V": ("#.#", "#.#", "#.#", "#.#", ".#."),
    "W": ("#.#", "#.#", "###", "###", "#.#"),
    "X": ("#.#", "#.#", ".#.", "#.#", "#.#"),
    "Y": ("#.#", "#.#", ".#.", ".#.", ".#."),
    "Z": ("###", "..#", ".#.", "#..", "###"),
    "0": ("###", "#.#", "#.#", "#.#", "###"),
    "1": (".#.", "##.", ".#.", ".#.", "###"),
    "2": ("##.", "..#", ".#.", "#..", "###"),
    "3": ("##.", "..#", ".#.", "..#", "##."),
    "4": ("#.#", "#.#", "###", "..#", "..#"),
    "5": ("###", "#..", "##.", "..#", "##."),
    "6": (".##", "#..", "###", "#.#", "###"),
    "7": ("###", "..#", ".#.", "#..", "#.."),
    "8": ("###", "#.#", "###", "#.#", "###"),
    "9": ("###", "#.#", "###", "..#", "##."),
    "-": ("...", "...", "###", "...", "..."),
    "_": ("...", "...", "...", "...", "###"),
    ".": ("...", "...", "...", "...", ".#."),
    ",": ("...", "...", "...", ".#.", "#.."),
    "+": ("...", ".#.", "###", ".#.", "..."),
    "(": ("..#", ".#.", ".#.", ".#.", "..#"),
    ")": ("#..", ".#.", ".#.", ".#.", "#.."),
    "!": (".#.", ".#.", ".#.", "...", ".#."),
    "?": ("##.", "..#", ".#.", "...", ".#."),
    "'": (".#.", ".#.", "...", "...", "..."),
    "/": ("..#", "..#", ".#.", "#..", "#.."),
    "&": (".#.", "#.#", ".#.", "#.#", ".##"),
    "#": ("#.#", "###", "#.#", "###", "#.#"),
    "%": ("#.#", "..#", ".#.", "#..", "#.#"),
    "=": ("...", "###", "...", "###", "..."),
    ":": ("...", ".#.", "...", ".#.", "..."),
    " ": ("...", "...", "...", "...", "..."),
}

FALLBACK = "?"
GLYPH_HEIGHT = 5
GLYPH_WIDTH = 3
MATRIX_HEIGHT = 8


def _glyph_columns(char: str, top: int) -> list[list[bool]]:
    """Return GLYPH_WIDTH columns of MATRIX_HEIGHT booleans for one character."""
    rows = GLYPHS.get(char.upper(), GLYPHS[FALLBACK])
    columns: list[list[bool]] = []
    for x in range(GLYPH_WIDTH):
        column = [False] * MATRIX_HEIGHT
        for y in range(GLYPH_HEIGHT):
            row_index = top + y
            if 0 <= row_index < MATRIX_HEIGHT:
                column[row_index] = rows[y][x] == "#"
        columns.append(column)
    return columns


def render_text(text: str, top: int = 2, spacing: int = 1) -> list[list[bool]]:
    """Render text to a list of columns, each a list of 8 row booleans.

    top      -- row index the 5-pixel-tall glyphs start at (2 centres them).
    spacing  -- blank columns inserted between characters.
    """
    blank = [False] * MATRIX_HEIGHT
    columns: list[list[bool]] = []
    for index, char in enumerate(text):
        if index:
            columns.extend([list(blank) for _ in range(spacing)])
        columns.extend(_glyph_columns(char, top))
    return columns


def blank_columns(count: int) -> list[list[bool]]:
    """Return `count` empty columns, used to pad a scroll in and out."""
    return [[False] * MATRIX_HEIGHT for _ in range(count)]
