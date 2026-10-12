"""Config: flat v1 keys + additive v0.2 sections ([weather], [cooling]).

Old config files keep working: missing sections fall back to defaults, and
the defaults are everything-off for actuation (v0.1 behavior preserved).
"""

from __future__ import annotations

import os
try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # py3.9/3.10
    import tomli as tomllib

from pathlib import Path

_KNOWN_THEMES = set()  # filled lazily to avoid a UI import at config load

DEFAULTS = {
    "theme": "predawn",
    "interval": 1.0,
    "layout": "auto",
    "fahrenheit": False,
    "paint_bg": True,
    "sort": "pid",
    "filter_me": False,
    "graph_style": "btop",
}

WEATHER_DEFAULTS = {
    "enabled": True,
    "lat": 1.3521,          # Singapore -- hot, humid, tropical; set yours
    "lon": 103.8198,
    "label": "Singapore, SG",
    "refresh_min": 20.0,
}

COOLING_DEFAULTS = {
    "enabled": False,        # opt-in only; also flip mode to dry_run/auto
    "mode": "off",           # off | dry_run | auto | manual
    "trigger_temp": 82.0,
    "resume_temp": 78.0,
    "min_delta_c": 8.0,
    "min_on_s": 120.0,
    "min_off_s": 180.0,
    "max_duty_min": 90.0,
    "trigger": "temp",       # temp | usage (usage: ON at load, OFF after idle_off_s)
    "usage_util": 5.0,       # % GPU util that counts as "in use"
    "idle_off_s": 600.0,     # continuous idle seconds before OFF (usage trigger)
    "plugs": [],             # [[cooling.plugs]] entries, see README
}


def config_path() -> Path:
    return Path.home() / ".config" / "zionstats" / "config.toml"


def _quote(v) -> str:
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


class Config:
    def __init__(self) -> None:
        self.values = dict(DEFAULTS)
        self.weather = dict(WEATHER_DEFAULTS)
        self.cooling = dict(COOLING_DEFAULTS)
        p = config_path()
        if p.exists():
            try:
                data = tomllib.loads(p.read_text())
                for k in DEFAULTS:
                    if k in data:
                        self.values[k] = data[k]
                for k in WEATHER_DEFAULTS:
                    if k in data.get("weather", {}):
                        self.weather[k] = data["weather"][k]
                sec = data.get("cooling", {})
                for k in COOLING_DEFAULTS:
                    if k in sec:
                        self.cooling[k] = sec[k]
                # coerce last, so file-loaded section values are sanitized too
                self._coerce()
            except Exception:
                pass

    def _coerce(self) -> None:
        """Hand-edited TOML must not crash the TUI at startup."""
        try:
            self.values["interval"] = max(0.2, min(30.0, float(self.values["interval"])))
        except (TypeError, ValueError):
            self.values["interval"] = DEFAULTS["interval"]
        for key in ("theme", "layout", "sort"):
            if not isinstance(self.values[key], str):
                self.values[key] = DEFAULTS[key]
        if self.values["theme"] not in _KNOWN_THEMES:
            self.values["theme"] = DEFAULTS["theme"]
        if self.values["layout"] not in ("auto", "hero", "grid", "compact"):
            self.values["layout"] = "auto"
        if self.values["sort"] not in ("pid", "user", "mem", "sm", "cpu"):
            self.values["sort"] = "pid"
        try:
            wlat, wlon = float(self.weather.get("lat")), float(self.weather.get("lon"))
            self.weather["lat"], self.weather["lon"] = wlat, wlon
        except (TypeError, ValueError):
            self.weather["lat"] = WEATHER_DEFAULTS["lat"]
            self.weather["lon"] = WEATHER_DEFAULTS["lon"]
        try:
            self.weather["refresh_min"] = float(self.weather.get("refresh_min"))
        except (TypeError, ValueError):
            self.weather["refresh_min"] = WEATHER_DEFAULTS["refresh_min"]
        for key in ("trigger_temp", "resume_temp", "min_delta_c",
                    "min_on_s", "min_off_s", "max_duty_min",
                    "usage_util", "idle_off_s"):
            try:
                self.cooling[key] = float(self.cooling[key])
            except (TypeError, ValueError):
                self.cooling[key] = COOLING_DEFAULTS[key]
        if self.values.get("graph_style") not in ("btop", "block", "tty", "braille", "ascii"):
            self.values["graph_style"] = DEFAULTS["graph_style"]
        if self.cooling.get("mode") not in ("off", "dry_run", "auto", "manual"):
            self.cooling["mode"] = COOLING_DEFAULTS["mode"]
        if self.cooling.get("trigger") not in ("temp", "usage"):
            self.cooling["trigger"] = COOLING_DEFAULTS["trigger"]
        if not isinstance(self.cooling.get("plugs", []), list):
            self.cooling["plugs"] = []

    def __getattr__(self, k: str):
        try:
            return self.values[k]
        except KeyError as e:  # pragma: no cover
            raise AttributeError(k) from e

    def set(self, key: str, value) -> None:
        """Flat key ('theme') or dotted section key ('cooling.mode')."""
        if "." in key:
            sec, sub = key.split(".", 1)
            table = {"weather": self.weather, "cooling": self.cooling}.get(sec)
            if table is not None and sub in table:
                table[sub] = value
                self.save()
            return
        if key in DEFAULTS:
            self.values[key] = value
            self.save()

    def save(self) -> None:
        p = config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# zionstats config", ""]
        for k, v in self.values.items():
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, str):
                lines.append(f"{k} = {_quote(v)}")
            else:
                lines.append(f"{k} = {v}")
        lines += ["", "[weather]"]
        for k, v in self.weather.items():
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, str):
                lines.append(f"{k} = {_quote(v)}")
            else:
                lines.append(f"{k} = {v}")
        lines += ["", "[cooling]"]
        plugs = self.cooling.get("plugs", [])
        for k, v in self.cooling.items():
            if k == "plugs":
                continue
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, str):
                lines.append(f"{k} = {_quote(v)}")
            else:
                lines.append(f"{k} = {v}")
        for plug in plugs:
            lines += ["", "[[cooling.plugs]]"]
            for k, v in plug.items():
                if isinstance(v, bool):
                    lines.append(f"{k} = {'true' if v else 'false'}")
                elif isinstance(v, (int, float)):
                    lines.append(f"{k} = {v}")
                else:
                    lines.append(f"{k} = {_quote(v)}")
        p.write_text("\n".join(lines) + "\n")
        try:
            os.chmod(p, 0o600)  # config can hold plug tokens / local keys
        except OSError:
            pass
