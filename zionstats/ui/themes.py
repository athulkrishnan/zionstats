"""Theme system: palette slots, hex->xterm-256 quantizer, 16 built-in themes.

All colors are hex strings ("#rrggbb").  They are quantized to the nearest
xterm-256 palette entry (indices 16-255 only, so terminal schemes that
remap the base 16 never distort the theme).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# base-16 ANSI palette (xterm defaults), used for <256-color degradation
_ANSI16 = [
    (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
    (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
    (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
]


# ---------------------------------------------------------------- color utils

def _rgb(hexstr: str) -> tuple[int, int, int]:
    h = hexstr.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hex(rgb: tuple[int, int, int]) -> str:
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def mix(a: str, b: str, t: float) -> str:
    ar, ag, ab = _rgb(a)
    br, bg_, bb = _rgb(b)
    return _hex((
        round(ar + (br - ar) * t),
        round(ag + (bg_ - ag) * t),
        round(ab + (bb - ab) * t),
    ))


def lighten(c: str, t: float) -> str:
    return mix(c, "#ffffff", t)


def darken(c: str, t: float) -> str:
    return mix(c, "#000000", t)


_CUBE = [0, 95, 135, 175, 215, 255]
_PALETTE: list[tuple[int, int, int]] = (
    [(_CUBE[r], _CUBE[g], _CUBE[b]) for r in range(6) for g in range(6) for b in range(6)]
    + [(v, v, v) for v in range(8, 248, 10)]          # 232..255 grayscale
)

_QCACHE: dict[str, int] = {}


def quantize256(hexstr: str) -> int:
    """Nearest xterm-256 index (always >= 16 so terminal schemes can't skew it)."""
    got = _QCACHE.get(hexstr)
    if got is not None:
        return got
    r, g, b = _rgb(hexstr)
    best, best_d = 16, 1 << 30
    for i, (pr, pg, pb) in enumerate(_PALETTE):
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if d < best_d:
            best, best_d = 16 + i, d
    _QCACHE[hexstr] = best
    return best


def gradient256(stops: tuple[str, ...], levels: int = 10) -> list[int]:
    """Discretize a multi-stop hex gradient into `levels` 256-color steps."""
    out: list[int] = []
    n = len(stops) - 1
    for i in range(levels):
        t = i / (levels - 1) if levels > 1 else 0.0
        seg = min(int(t * n), n - 1)
        local = t * n - seg
        out.append(quantize256(mix(stops[seg], stops[seg + 1], local)))
    return out


# ---------------------------------------------------------------- theme model

@dataclass(frozen=True)
class Theme:
    name: str
    bg: str
    fg: str
    dim: str
    border: str
    border_sel: str
    title: str
    header_bg: str
    header_fg: str
    grad: tuple[str, ...]
    grad_mem: tuple[str, ...]
    grad_power: tuple[str, ...]
    grad_temp: tuple[str, ...]
    bar: str
    bar_track: str
    ok: str
    warn: str
    crit: str
    accent: str
    accent2: str
    table_alt: str
    table_head: str
    key_hint: str
    key_label: str
    light: bool = False

    # quantized views (filled lazily)
    _q: dict = field(default_factory=dict, repr=False, compare=False)

    def q(self, slot: str) -> int:
        v = self._q.get(slot)
        if v is None:
            v = quantize256(getattr(self, slot))
            self._q[slot] = v
        return v

    def ansi16(self, slot: str) -> int:
        """Nearest of the 16 base ANSI colors -- for <256-color terminals."""
        key = f"a16:{slot}"
        v = self._q.get(key)
        if v is None:
            r, g, b = _rgb(getattr(self, slot))
            best, best_d = 7, 1 << 30
            for i, (pr, pg, pb) in enumerate(_ANSI16):
                d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
                if d < best_d:
                    best, best_d = i, d
            v = best
            self._q[key] = v
        return v

    def grad_q(self, which: str = "grad", levels: int = 10) -> list[int]:
        key = f"gradq:{which}:{levels}"
        v = self._q.get(key)
        if v is None:
            v = gradient256(getattr(self, which), levels)
            self._q[key] = v
        return v

    def level_color(self, which: str, frac: float) -> int:
        """256-color for `frac` (0..1) along a named gradient."""
        g = self.grad_q(which)
        return g[max(0, min(len(g) - 1, int(frac * (len(g) - 1) + 0.5)))]


def _theme(
    name: str,
    bg: str,
    fg: str,
    dim: str,
    border: str,
    accent: str,
    ok: str,
    warn: str,
    crit: str,
    accent2: str | None = None,
    grad: tuple[str, ...] | None = None,
    grad_mem: tuple[str, ...] | None = None,
    grad_power: tuple[str, ...] | None = None,
    grad_temp: tuple[str, ...] | None = None,
    title: str | None = None,
) -> Theme:
    accent2 = accent2 or accent
    sel = title or accent
    return Theme(
        name=name,
        light=(name == "paperwhite"),
        bg=bg,
        fg=fg,
        dim=dim,
        border=border,
        border_sel=sel,
        title=title or accent,
        header_bg=mix(bg, accent, 0.18),
        header_fg=lighten(fg, 0.25),
        grad=grad or (darken(ok, 0.55), mix(ok, bg, 0.35), ok, warn, lighten(warn, 0.3)),
        grad_mem=grad_mem or (darken(accent2, 0.55), mix(accent2, bg, 0.35), accent2, warn, crit),
        grad_power=grad_power or (darken(warn, 0.55), mix(warn, bg, 0.35), warn, ok, accent),
        grad_temp=grad_temp or (darken(ok, 0.55), ok, warn, darken(crit, 0.15), crit),
        bar=ok,
        bar_track=mix(bg, fg, 0.10),
        ok=ok,
        warn=warn,
        crit=crit,
        accent=accent,
        accent2=accent2,
        table_alt=mix(bg, fg, 0.045),
        table_head=mix(fg, bg, 0.35),
        key_hint=accent,
        key_label=mix(fg, bg, 0.30),
    )


# ---------------------------------------------------------------- 16 themes

def _build_registry() -> dict[str, Theme]:
    t: dict[str, Theme] = {}

    t["predawn"] = _theme(
        "predawn", bg="#212121", fg="#d8d8d3", dim="#7a7a72", border="#3a3a38",
        accent="#cf9f50", accent2="#b085c8",
        ok="#95c24b", warn="#cf9f50", crit="#d65d4e",
        grad=("#39473f", "#5f7a4c", "#8bb04f", "#b3cf62", "#e3ed9a"),
        grad_mem=("#4a4030", "#8a7040", "#cf9f50", "#e8c070", "#f8e8b0"),
        grad_power=("#4a3527", "#8a5a30", "#cf7f40", "#e8a050", "#f8d090"),
        grad_temp=("#2f4a4a", "#3f7a72", "#4fa08f", "#c8964f", "#e05545"),
    )
    t["solarized-dark"] = _theme(
        "solarized-dark", bg="#002b36", fg="#93a1a1", dim="#586e75", border="#073642",
        accent="#b58900", accent2="#6c71c4",
        ok="#859900", warn="#b58900", crit="#dc322f",
        grad=("#073642", "#586e75", "#2aa198", "#859900", "#b58900"),
        grad_mem=("#073642", "#657b83", "#268bd2", "#6c71c4", "#d33682"),
        grad_power=("#3b3200", "#657b83", "#b58900", "#859900", "#2aa198"),
        grad_temp=("#073642", "#2aa198", "#b58900", "#cb4b16", "#dc322f"),
    )
    t["gruvbox"] = _theme(
        "gruvbox", bg="#282828", fg="#ebdbb2", dim="#928374", border="#504945",
        accent="#fabd2f", accent2="#d3869b",
        ok="#b8bb26", warn="#fe8019", crit="#fb4934",
        grad=("#3c3836", "#689d6a", "#b8bb26", "#fabd2f", "#fe8019"),
        grad_mem=("#3c3836", "#458588", "#83a598", "#d3869b", "#fb4934"),
        grad_power=("#3c3836", "#d65d0e", "#fe8019", "#fabd2f", "#fb4934"),
        grad_temp=("#3c3836", "#689d6a", "#fabd2f", "#fe8019", "#fb4934"),
    )
    t["nord"] = _theme(
        "nord", bg="#2e3440", fg="#eceff4", dim="#7b88a1", border="#3b4252",
        accent="#88c0d0", accent2="#b48ead",
        ok="#a3be8c", warn="#ebcb8b", crit="#bf616a",
        grad=("#3b4252", "#4c566a", "#81a1c1", "#88c0d0", "#8fbcbb"),
        grad_mem=("#3b4252", "#5e81ac", "#81a1c1", "#b48ead", "#bf616a"),
        grad_power=("#3b4252", "#d08770", "#ebcb8b", "#a3be8c", "#8fbcbb"),
        grad_temp=("#3b4252", "#a3be8c", "#ebcb8b", "#d08770", "#bf616a"),
    )
    t["dracula"] = _theme(
        "dracula", bg="#282a36", fg="#f8f8f2", dim="#6272a4", border="#44475a",
        accent="#bd93f9", accent2="#ff79c6",
        ok="#50fa7b", warn="#f1fa8c", crit="#ff5555",
        grad=("#44475a", "#6272a4", "#50fa7b", "#f1fa8c", "#ff79c6"),
        grad_mem=("#44475a", "#8be9fd", "#bd93f9", "#ff79c6", "#ff5555"),
        grad_power=("#44475a", "#ffb86c", "#ff79c6", "#bd93f9", "#8be9fd"),
        grad_temp=("#44475a", "#50fa7b", "#f1fa8c", "#ffb86c", "#ff5555"),
    )
    t["catppuccin-mocha"] = _theme(
        "catppuccin-mocha", bg="#1e1e2e", fg="#cdd6f4", dim="#6c7086", border="#313244",
        accent="#cba6f7", accent2="#f5c2e7",
        ok="#a6e3a1", warn="#f9e2af", crit="#f38ba8",
        grad=("#313244", "#45475a", "#89b4fa", "#a6e3a1", "#f9e2af"),
        grad_mem=("#313244", "#89dceb", "#94e2d5", "#cba6f7", "#f38ba8"),
        grad_power=("#313244", "#fab387", "#f9e2af", "#a6e3a1", "#89dceb"),
        grad_temp=("#313244", "#a6e3a1", "#f9e2af", "#fab387", "#f38ba8"),
    )
    t["tokyo-night"] = _theme(
        "tokyo-night", bg="#1a1b26", fg="#c0caf5", dim="#565f89", border="#292e42",
        accent="#7aa2f7", accent2="#bb9af7",
        ok="#9ece6a", warn="#e0af68", crit="#f7768e",
        grad=("#292e42", "#414868", "#7aa2f7", "#9ece6a", "#e0af68"),
        grad_mem=("#292e42", "#7dcfff", "#73daca", "#bb9af7", "#f7768e"),
        grad_power=("#292e42", "#ff9e64", "#e0af68", "#9ece6a", "#7dcfff"),
        grad_temp=("#292e42", "#9ece6a", "#e0af68", "#ff9e64", "#f7768e"),
    )
    t["synthwave-84"] = _theme(
        "synthwave-84", bg="#241b2f", fg="#f8f8f2", dim="#848bbd", border="#495495",
        accent="#ff7edb", accent2="#36f9f6",
        ok="#72f1b8", warn="#fede5d", crit="#fe4450",
        grad=("#2e2440", "#7148fb", "#36f9f6", "#72f1b8", "#fede5d"),
        grad_mem=("#2e2440", "#848bbd", "#ff7edb", "#fe4450", "#fede5d"),
        grad_power=("#2e2440", "#f97e72", "#fede5d", "#72f1b8", "#36f9f6"),
        grad_temp=("#2e2440", "#72f1b8", "#fede5d", "#f97e72", "#fe4450"),
    )
    t["cyberpunk"] = _theme(
        "cyberpunk", bg="#0d0d14", fg="#e5e5e5", dim="#6e6e7a", border="#2a2a3a",
        accent="#fcee0a", accent2="#ff2a6d",
        ok="#00ff9f", warn="#fcee0a", crit="#ff003c",
        grad=("#1a1a2a", "#00445f", "#00e5ff", "#00ff9f", "#fcee0a"),
        grad_mem=("#1a1a2a", "#7a2a8a", "#ff2a6d", "#ff9de2", "#00e5ff"),
        grad_power=("#1a1a2a", "#4a3a00", "#ff9e00", "#fcee0a", "#00ff9f"),
        grad_temp=("#1a1a2a", "#00ff9f", "#fcee0a", "#ff9e00", "#ff003c"),
    )
    t["everforest"] = _theme(
        "everforest", bg="#2d353b", fg="#d3c6aa", dim="#859289", border="#414b50",
        accent="#a7c080", accent2="#d699b6",
        ok="#a7c080", warn="#dbbc7f", crit="#e67e80",
        grad=("#414b50", "#5a6c66", "#83b092", "#a7c080", "#dbbc7f"),
        grad_mem=("#414b50", "#3d5a66", "#7fbbb3", "#d699b6", "#e67e80"),
        grad_power=("#414b50", "#e69875", "#dbbc7f", "#a7c080", "#7fbbb3"),
        grad_temp=("#414b50", "#a7c080", "#dbbc7f", "#e69875", "#e67e80"),
    )
    t["rose-pine"] = _theme(
        "rose-pine", bg="#191724", fg="#e0def4", dim="#908caa", border="#26233a",
        accent="#c4a7e7", accent2="#ebbcba",
        ok="#9ccfd8", warn="#f6c177", crit="#eb6f92",
        grad=("#26233a", "#403d52", "#31748f", "#9ccfd8", "#f6c177"),
        grad_mem=("#26233a", "#31748f", "#c4a7e7", "#eb6f92", "#f6c177"),
        grad_power=("#26233a", "#ebbcba", "#f6c177", "#9ccfd8", "#c4a7e7"),
        grad_temp=("#26233a", "#9ccfd8", "#f6c177", "#ebbcba", "#eb6f92"),
    )
    t["one-dark"] = _theme(
        "one-dark", bg="#282c34", fg="#abb2bf", dim="#5c6370", border="#3e4451",
        accent="#61afef", accent2="#c678dd",
        ok="#98c379", warn="#e5c07b", crit="#e06c75",
        grad=("#3e4451", "#4b5263", "#56b6c2", "#98c379", "#e5c07b"),
        grad_mem=("#3e4451", "#61afef", "#c678dd", "#e06c75", "#e5c07b"),
        grad_power=("#3e4451", "#d19a66", "#e5c07b", "#98c379", "#56b6c2"),
        grad_temp=("#3e4451", "#98c379", "#e5c07b", "#d19a66", "#e06c75"),
    )
    t["matrix"] = _theme(
        "matrix", bg="#030b04", fg="#00b34a", dim="#0a5f28", border="#0a3018",
        accent="#00ff66", accent2="#66ff99",
        ok="#00cc55", warn="#aaff00", crit="#ff4455",
        grad=("#041a08", "#0a5f28", "#00b34a", "#00ff66", "#c8ffd4"),
        grad_mem=("#041a08", "#0a5f28", "#00b34a", "#00ff66", "#c8ffd4"),
        grad_power=("#041a08", "#0a5f28", "#00b34a", "#00ff66", "#c8ffd4"),
        grad_temp=("#041a08", "#0a5f28", "#00b34a", "#aaff00", "#ff5555"),
    )
    t["amber-crt"] = _theme(
        "amber-crt", bg="#140d02", fg="#ffb000", dim="#7a5400", border="#3a2a08",
        accent="#ffd75f", accent2="#ffaa33",
        ok="#ffb000", warn="#ff8c00", crit="#ff4d00",
        grad=("#2e1f04", "#7a5400", "#c98a00", "#ffb000", "#ffe6a6"),
        grad_mem=("#2e1f04", "#7a5400", "#c98a00", "#ffb000", "#ffe6a6"),
        grad_power=("#2e1f04", "#7a5400", "#c98a00", "#ffb000", "#ffe6a6"),
        grad_temp=("#2e1f04", "#c98a00", "#ffb000", "#ff8c00", "#ff4d00"),
    )
    t["btop-classic"] = _theme(
        "btop-classic", bg="#0f131a", fg="#ccced3", dim="#7a828e", border="#37474f",
        accent="#29b6f6", accent2="#e91e63",
        ok="#4caf50", warn="#ff9800", crit="#f44336",
        grad=("#263238", "#2e7d32", "#8bc34a", "#ffeb3b", "#ff9800"),
        grad_mem=("#263238", "#0277bd", "#29b6f6", "#ab47bc", "#f44336"),
        grad_power=("#263238", "#ff7043", "#ffa726", "#ffee58", "#66bb6a"),
        grad_temp=("#263238", "#66bb6a", "#ffee58", "#ffa726", "#ef5350"),
    )
    t["paperwhite"] = _theme(
        "paperwhite", bg="#fafafa", fg="#1f2430", dim="#8b93a7", border="#d5dae3",
        accent="#2b6cb0", accent2="#6b46c1",
        ok="#2f855a", warn="#b7791f", crit="#c53030",
        grad=("#e2e8f0", "#a0aec0", "#63b3ed", "#48bb78", "#2f855a"),
        grad_mem=("#e2e8f0", "#63b3ed", "#805ad5", "#d53f8c", "#c53030"),
        grad_power=("#e2e8f0", "#ed8936", "#ecc94b", "#48bb78", "#2b6cb0"),
        grad_temp=("#e2e8f0", "#48bb78", "#ecc94b", "#ed8936", "#c53030"),
    )
    return t


REGISTRY: dict[str, Theme] = _build_registry()
THEME_ORDER: list[str] = list(REGISTRY)
DEFAULT_THEME = "predawn"


def get_theme(name: str) -> Theme:
    return REGISTRY.get(name, REGISTRY[DEFAULT_THEME])


def next_theme(name: str, step: int = 1) -> str:
    i = THEME_ORDER.index(name) if name in THEME_ORDER else 0
    return THEME_ORDER[(i + step) % len(THEME_ORDER)]
