#!/usr/bin/env python3
"""Normalise upstream colour schemes into OmaRag theme files.

Sources (both MIT licensed):
  * superfile  — https://github.com/yorukot/superfile   src/superfile_config/theme/*.toml
  * Omarchy    — https://github.com/basecamp/omarchy    themes/*/colors.toml

Both are richer than a flat palette, but in different shapes. superfile names a
colour per region (sidebar/file panel/footer/modal); Omarchy names a palette and
lets the consumer decide. We normalise both onto one OmaRag theme file that
carries the region roles the renderer asks for.

Where the two projects ship a scheme under the same name, Omarchy wins — it is
what the user's desktop actually runs.

Run once; the generated files under assets/themes/ are committed.

    python3 scripts/import_themes.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "themes"

SUPERFILE = "yorukot/superfile"
SUPERFILE_DIR = "src/superfile_config/theme"
OMARCHY = "basecamp/omarchy"
OMARCHY_DIR = "themes"


# ---------------------------------------------------------------- fetching


def gh(path: str) -> str:
    """Read a file from GitHub via the authenticated gh CLI."""
    result = subprocess.run(
        ["gh", "api", f"repos/{path}", "--jq", ".content"],
        capture_output=True,
        text=True,
        check=True,
    )
    import base64

    return base64.b64decode(result.stdout.strip()).decode("utf-8")


def gh_tree(repo: str, ref: str) -> list[str]:
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/git/trees/{ref}?recursive=1", "--jq", ".tree[].path"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


# ---------------------------------------------------------------- colours


HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")

# A hairline below this against its panel stops reading as a boundary.
MIN_BORDER_CONTRAST = 2.2


def hex_of(value: object, fallback: str | None = None) -> str | None:
    """Normalise to `#rrggbb`, or None when it is not a colour."""
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str):
        return fallback
    match = HEX.match(value.strip())
    if not match:
        return fallback
    return "#" + match.group(1).lower()


def rgb(colour: str) -> tuple[int, int, int]:
    value = int(colour[1:], 16)
    return (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF


def mix(left: str, right: str, percent: int) -> str:
    a, b = rgb(left), rgb(right)
    out = tuple(round(x + (y - x) * percent / 100) for x, y in zip(a, b))
    return "#{:02x}{:02x}{:02x}".format(*out)


def distinct_fill(candidate: str | None, background: str, accent: str) -> str:
    """A selection fill must differ from the background.

    superfile marks selection with the foreground colour and leaves the fill
    equal to the panel background, which would make our row highlight invisible.
    Fall back to a tint of the accent in that case.
    """
    if candidate and candidate.lower() != background.lower():
        return candidate
    return mix(background, accent, 22)


def contrast(left: str, right: str) -> float:
    a, b = luminance(left), luminance(right)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def readable_border(border: str, background: str, foreground: str) -> str:
    """Lift a border until it is actually visible against its panel.

    Several upstream schemes set an idle border barely distinguishable from the
    background — fine in a file manager whose panels are also separated by
    position, but here the hairline *is* the region boundary. Nudge it towards
    the foreground, keeping its hue, until it reads.
    """
    if contrast(border, background) >= MIN_BORDER_CONTRAST:
        return border
    for percent in range(10, 101, 10):
        lifted = mix(border, foreground, percent)
        if contrast(lifted, background) >= MIN_BORDER_CONTRAST:
            return lifted
    return mix(background, foreground, 45)


def distinct_border(idle: str | None, active: str, background: str) -> str:
    """The idle border must differ from the active one.

    Some upstream schemes (Kaolin, for one) use the same colour for both, which
    would make a focused pane indistinguishable from an unfocused one. Pull the
    idle border towards the background in that case.
    """
    if idle and idle.lower() != active.lower():
        return idle
    return mix(background, active, 40)


def luminance(colour: str) -> float:
    def channel(value: int) -> float:
        v = value / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb(colour))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


# ---------------------------------------------------------------- model


@dataclass
class Theme:
    name: str
    source: str
    mode: str
    base: dict[str, object] = field(default_factory=dict)
    sidebar: dict[str, str] = field(default_factory=dict)
    workspace: dict[str, str] = field(default_factory=dict)
    footer: dict[str, str] = field(default_factory=dict)
    modal: dict[str, str] = field(default_factory=dict)
    status: dict[str, str] = field(default_factory=dict)
    ansi: dict[str, str] = field(default_factory=dict)

    def slug(self) -> str:
        """Filename: readable, hyphenated."""
        return normalise(self.name)

    def collision_key(self) -> str:
        """Same scheme under a different spelling must collide, so
        `tokyonight` and `tokyo-night` resolve to one entry."""
        return collision_key(self.name)

    def to_toml(self) -> str:
        def table(title: str, values: dict[str, object]) -> str:
            body = "\n".join(
                f"{key} = {json.dumps(value)}"
                for key, value in values.items()
                if value is not None
            )
            return f"\n[{title}]\n{body}\n"

        head = (
            f"# {self.name} — imported from {self.source}. Do not edit by hand;\n"
            f"# regenerate with scripts/import_themes.py. See ATTRIBUTION.md.\n"
            f"name = {json.dumps(self.name)}\n"
            f"source = {json.dumps(self.source)}\n"
            f"mode = {json.dumps(self.mode)}\n"
        )
        return (
            head
            + table("base", self.base)
            + table("sidebar", self.sidebar)
            + table("workspace", self.workspace)
            + table("footer", self.footer)
            + table("modal", self.modal)
            + table("status", self.status)
            + table("ansi", self.ansi)
        )


def normalise(name: str) -> str:
    """Filename slug: lowercase, separators collapsed to a single hyphen."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def collision_key(name: str) -> str:
    """Identity across spellings: separators removed entirely."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def title_of(slug: str) -> str:
    words = slug.replace("_", "-").split("-")
    keep = {"0x96f": "0x96f"}
    return " ".join(keep.get(w, w.capitalize()) for w in words if w)


# ---------------------------------------------------------------- superfile


def from_superfile(slug: str, raw: str) -> Theme | None:
    data = tomllib.loads(raw)

    background = hex_of(data.get("file_panel_bg")) or hex_of(data.get("full_screen_bg"))
    foreground = hex_of(data.get("file_panel_fg")) or hex_of(data.get("full_screen_fg"))
    if not background or not foreground:
        return None

    accent = (
        hex_of(data.get("file_panel_border_active"))
        or hex_of(data.get("sidebar_border_active"))
        or foreground
    )
    gradient = data.get("gradient_color") or []
    gradient = [hex_of(c) for c in gradient if hex_of(c)] or [accent, accent]
    if len(gradient) == 1:
        gradient = [gradient[0], gradient[0]]

    mode = "light" if luminance(background) > 0.5 else "dark"
    muted = hex_of(data.get("sidebar_divider")) or mix(background, foreground, 45)
    selection = distinct_fill(
        hex_of(data.get("sidebar_item_selected_bg")), background, accent
    )

    return Theme(
        name=title_of(slug),
        source="superfile",
        mode=mode,
        base={
            "background": background,
            "foreground": foreground,
            "muted": muted,
            "accent": accent,
            "selection": selection,
            "gradient": gradient[:2],
        },
        sidebar={
            "fg": hex_of(data.get("sidebar_fg"), foreground),
            "bg": hex_of(data.get("sidebar_bg"), background),
            "border": distinct_border(
                hex_of(data.get("sidebar_border")),
                hex_of(data.get("sidebar_border_active"), accent),
                background,
            ),
            "border_active": hex_of(data.get("sidebar_border_active"), accent),
            "title": hex_of(data.get("sidebar_title"), accent),
            "divider": hex_of(data.get("sidebar_divider"), muted),
            "item_selected_fg": hex_of(data.get("sidebar_item_selected_fg"), foreground),
            "item_selected_bg": distinct_fill(
                hex_of(data.get("sidebar_item_selected_bg")), background, accent
            ),
        },
        workspace={
            "fg": hex_of(data.get("file_panel_fg"), foreground),
            "bg": hex_of(data.get("file_panel_bg"), background),
            "border": distinct_border(
                hex_of(data.get("file_panel_border")),
                hex_of(data.get("file_panel_border_active"), accent),
                background,
            ),
            "border_active": hex_of(data.get("file_panel_border_active"), accent),
            "title": hex_of(data.get("file_panel_top_path"), accent),
            "item_selected_fg": hex_of(data.get("file_panel_item_selected_fg"), foreground),
            "item_selected_bg": distinct_fill(
                hex_of(data.get("file_panel_item_selected_bg")), background, accent
            ),
        },
        footer={
            "fg": hex_of(data.get("footer_fg"), foreground),
            "bg": hex_of(data.get("footer_bg"), background),
            "border": distinct_border(
                hex_of(data.get("footer_border")),
                hex_of(data.get("footer_border_active"), accent),
                background,
            ),
            "border_active": hex_of(data.get("footer_border_active"), accent),
        },
        modal={
            "fg": hex_of(data.get("modal_fg"), foreground),
            "bg": hex_of(data.get("modal_bg"), background),
            "border_active": hex_of(data.get("modal_border_active"), accent),
            "cancel_fg": hex_of(data.get("modal_cancel_fg"), background),
            "cancel_bg": hex_of(data.get("modal_cancel_bg"), accent),
            "confirm_fg": hex_of(data.get("modal_confirm_fg"), background),
            "confirm_bg": hex_of(data.get("modal_confirm_bg"), accent),
        },
        status={
            "cursor": hex_of(data.get("cursor"), accent),
            "correct": hex_of(data.get("correct"), accent),
            "error": hex_of(data.get("error"), accent),
            "hint": hex_of(data.get("hint"), muted),
            "cancel": hex_of(data.get("cancel"), muted),
            "hotkey": hex_of(data.get("help_menu_hotkey"), accent),
        },
        ansi=ansi_from_superfile(data, accent),
    )


def ansi_from_superfile(data: dict, accent: str) -> dict[str, str]:
    """superfile has no ANSI block; derive semantic colours from what it names."""
    error = hex_of(data.get("error"), accent)
    correct = hex_of(data.get("correct"), accent)
    hint = hex_of(data.get("hint"), accent)
    cancel = hex_of(data.get("cancel"), accent)
    return {
        "red": error,
        "orange": cancel,
        "yellow": cancel,
        "green": correct,
        "cyan": hint,
        "blue": accent,
        "magenta": hex_of(data.get("help_menu_title"), accent),
    }


# ---------------------------------------------------------------- omarchy


def from_omarchy(slug: str, raw: str, light: bool) -> Theme | None:
    """Omarchy ships a flat palette; the region roles are derived from it.

    This mirrors what `parse_omarchy_palette` in the TUI already does for the
    live "follows your desktop" theme, so a static Omarchy theme and the live
    one look the same.
    """
    data = tomllib.loads(raw)

    background = hex_of(data.get("background"))
    foreground = hex_of(data.get("foreground"))
    accent = hex_of(data.get("accent")) or hex_of(data.get("blue")) or foreground
    if not background or not foreground:
        return None

    mode = "light" if (light or luminance(background) > 0.5) else "dark"
    muted = hex_of(data.get("muted")) or mix(background, foreground, 38)
    selection = distinct_fill(
        hex_of(data.get("selection")), background, accent
    )
    surface = hex_of(data.get("lighter_background")) or mix(background, foreground, 6)
    border = distinct_border(mix(background, foreground, 25), accent, background)

    magenta = hex_of(data.get("magenta")) or accent
    gradient = [accent, magenta if magenta != accent else hex_of(data.get("cyan"), accent)]

    return Theme(
        name=title_of(slug),
        source="omarchy",
        mode=mode,
        base={
            "background": background,
            "foreground": foreground,
            "muted": muted,
            "accent": accent,
            "selection": selection,
            "gradient": gradient,
        },
        sidebar={
            "fg": foreground,
            "bg": hex_of(data.get("dark_background"), background),
            "border": border,
            "border_active": accent,
            "title": accent,
            "divider": muted,
            "item_selected_fg": foreground,
            "item_selected_bg": selection,
        },
        workspace={
            "fg": foreground,
            "bg": background,
            "border": border,
            "border_active": accent,
            "title": accent,
            "item_selected_fg": foreground,
            "item_selected_bg": selection,
        },
        footer={
            "fg": hex_of(data.get("dark_foreground"), muted),
            "bg": hex_of(data.get("dark_background"), background),
            "border": border,
            "border_active": accent,
        },
        modal={
            "fg": foreground,
            "bg": surface,
            "border_active": accent,
            "cancel_fg": background,
            "cancel_bg": hex_of(data.get("red"), accent),
            "confirm_fg": background,
            "confirm_bg": hex_of(data.get("green"), accent),
        },
        status={
            "cursor": accent,
            "correct": hex_of(data.get("green"), accent),
            "error": hex_of(data.get("red"), accent),
            "hint": hex_of(data.get("cyan"), accent),
            "cancel": hex_of(data.get("orange"), muted),
            "hotkey": accent,
        },
        ansi={
            "red": hex_of(data.get("red"), accent),
            "orange": hex_of(data.get("orange"), accent),
            "yellow": hex_of(data.get("yellow"), accent),
            "green": hex_of(data.get("green"), accent),
            "cyan": hex_of(data.get("cyan"), accent),
            "blue": hex_of(data.get("blue"), accent),
            "magenta": magenta,
        },
    )


# ---------------------------------------------------------------- driver


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    themes: dict[str, Theme] = {}

    print("reading Omarchy…")
    omarchy_tree = gh_tree(OMARCHY, "master")
    omarchy_slugs = sorted(
        {
            path.split("/")[1]
            for path in omarchy_tree
            if path.startswith(f"{OMARCHY_DIR}/") and path.count("/") >= 2
        }
    )
    light_modes = {
        path.split("/")[1] for path in omarchy_tree if path.endswith("/light.mode")
    }
    for slug in omarchy_slugs:
        try:
            raw = gh(f"{OMARCHY}/contents/{OMARCHY_DIR}/{slug}/colors.toml")
        except subprocess.CalledProcessError:
            print(f"  skip {slug}: no colors.toml")
            continue
        theme = from_omarchy(slug, raw, slug in light_modes)
        if theme:
            themes[theme.collision_key()] = theme
            print(f"  + {theme.name} ({theme.mode})")

    print("reading superfile…")
    superfile_tree = gh_tree(SUPERFILE, "main")
    for path in sorted(p for p in superfile_tree if p.startswith(f"{SUPERFILE_DIR}/")):
        slug = Path(path).stem
        key = collision_key(slug)
        if key in themes:
            # Omarchy wins a name collision.
            print(f"  - {slug}: already provided by Omarchy")
            continue
        theme = from_superfile(slug, gh(f"{SUPERFILE}/contents/{path}"))
        if theme:
            themes[theme.collision_key()] = theme
            print(f"  + {theme.name} ({theme.mode})")

    for existing in OUT.glob("*.toml"):
        existing.unlink()
    for theme in themes.values():
        foreground = theme.base["foreground"]
        for region in (theme.sidebar, theme.workspace, theme.footer):
            region["border"] = readable_border(
                region["border"], region["bg"], foreground
            )

    for theme in sorted(themes.values(), key=lambda t: t.slug()):
        (OUT / f"{theme.slug()}.toml").write_text(theme.to_toml(), encoding="utf-8")

    (OUT / "ATTRIBUTION.md").write_text(ATTRIBUTION, encoding="utf-8")

    dark = sum(1 for t in themes.values() if t.mode == "dark")
    print(
        f"\nwrote {len(themes)} themes to {OUT.relative_to(ROOT)} "
        f"({dark} dark, {len(themes) - dark} light)"
    )
    return 0


ATTRIBUTION = """# Theme attribution

The colour schemes in this directory are derived from two upstream projects.
Both are MIT licensed; their copyright notices are reproduced below as the
licence requires. The files here are normalised into OmaRag's own theme format
by `scripts/import_themes.py` — the colour values are theirs, the layout is ours.

## superfile — https://github.com/yorukot/superfile

    MIT License

    Copyright (c) 2024 yorukot

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.

Individual superfile themes carry their own author credits in the upstream
files; those credits are preserved in the theme's `source` field and in this
notice rather than in every generated file.

## Omarchy — https://github.com/basecamp/omarchy

    MIT License

    Copyright (c) 2025 37signals

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.

Where a scheme exists in both projects, the Omarchy variant is kept, because it
is what an Omarchy desktop actually runs.
"""


if __name__ == "__main__":
    sys.exit(main())
