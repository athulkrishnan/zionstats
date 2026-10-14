"""Climate layer: keyless global weather (Open-Meteo), derived heat metrics,
and cooling-effectiveness insight.  Works anywhere on Earth; the shipped
defaults are neutral -- set your location via --location or [weather] lat/lon.

All network use is optional and cached: a fetch failure degrades to the last
cached observation with an age badge, never to a crash.
"""

from __future__ import annotations

import json
import math
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&current=temperature_2m,relative_humidity_2m,dew_point_2m,"
    "apparent_temperature,precipitation,weather_code,wind_speed_10m,is_day"
    "&hourly=temperature_2m&forecast_days=2&timezone=auto"
)

CACHE_PATH = Path.home() / ".cache" / "zionstats" / "weather.json"

WMO = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Showers", 81: "Showers", 82: "Violent showers",
    85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "T-storm + hail", 99: "T-storm + hail",
}


def wet_bulb_c(temp_c: float, rh: float) -> float:
    """Stull (2011) approximation; good for 5-99% RH, -20..50C."""
    rh = max(5.0, min(99.0, float(rh)))
    t = float(temp_c)
    return (
        t * math.atan(0.151977 * math.sqrt(rh + 8.313659))
        + math.atan(t + rh)
        - math.atan(rh - 1.676331)
        + 0.00391838 * rh ** 1.5 * math.atan(0.023101 * rh)
        - 4.686035
    )


@dataclass
class Weather:
    ts: float
    lat: float
    lon: float
    label: str = ""
    temp_c: float = 0.0
    rh: float = 0.0
    dew_c: float = 0.0
    apparent_c: float = 0.0
    precip_mm: float = 0.0
    code: int = 0
    wind_kmh: float = 0.0
    is_day: bool = True
    hourly: list[tuple[float, float]] = field(default_factory=list)  # (epoch, temp_c)
    stale: bool = False

    # ---- derived -------------------------------------------------------

    def age_s(self, now: float | None = None) -> float:
        return (now or time.time()) - self.ts

    def wet_bulb_c(self) -> float:
        return wet_bulb_c(self.temp_c, self.rh)

    def condition(self) -> str:
        return WMO.get(self.code, f"code {self.code}")

    def delta_vs(self, tgpu: float) -> float:
        """How much headroom ambient air has to carry heat away."""
        return tgpu - self.temp_c

    def effectiveness(self, tgpu: float) -> float:
        """0..1 -- is the blower worth it right now?

        Headroom (Tgpu - Tamb) normalized to 30C, derated by humidity:
        muggy air carries less cooling per watt of fan noise.
        """
        head = max(0.0, min(1.0, self.delta_vs(tgpu) / 30.0))
        humidity_penalty = 1.0 - 0.25 * max(0.0, min(1.0, (self.rh - 50.0) / 50.0))
        return head * humidity_penalty

    def condensation_risk(self, t_surface: float) -> bool:
        """Blowing ambient air at a surface below the dew point condenses
        water on it -- relevant for idle cards in cold AC rooms on muggy
        nights.  Rare, but worth a warning when it applies."""
        return t_surface < self.dew_c

    def advisories(self, tgpu: float | None, fahrenheit: bool = False) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        def t(c):
            return f"{c * 9 / 5 + 32:.0f}F" if fahrenheit else f"{c:.0f}C"
        if self.condensation_risk(tgpu or 0.0) and tgpu is not None:
            out.append(("warn", f"GPU below dew point ({t(self.dew_c)}) -- condensation risk, keep blower off"))
        wb = self.wet_bulb_c()
        if wb > 29.0:
            out.append(("warn", f"wet-bulb {t(wb)}: extreme humid heat, blower cooling is marginal"))
        elif wb > 26.0:
            out.append(("info", f"wet-bulb {t(wb)}: humid -- blower helps less than in dry air"))
        if self.delta_vs(tgpu or 0.0) < 8.0 and tgpu is not None and tgpu > 60:
            out.append(("info", f"ambient {t(self.temp_c)} close to GPU {t(tgpu)}: diminished cooling delta"))
        if self.code >= 95:
            out.append(("warn", "thunderstorm overhead -- consider unplugging during outages"))
        return out

    def summary_line(self) -> str:
        return f"{self.temp_c:.1f}C {self.rh:.0f}%RH dew {self.dew_c:.1f}C -- {self.condition()}"


# ---------------------------------------------------------------- provider


class WeatherProvider:
    """Open-Meteo fetch with atomic disk cache; tolerant of offline use."""

    def __init__(self, lat: float, lon: float, label: str = "",
                 refresh_min: float = 20.0, cache_path: Path | None = None) -> None:
        self.lat, self.lon, self.label = float(lat), float(lon), label
        self.refresh_s = refresh_min * 60.0
        self.cache_path = cache_path or CACHE_PATH
        self.last: Weather | None = None
        self.last_error: str | None = None
        self._last_fetch = 0.0
        cached = self._read_cache()
        if cached is not None:
            self.last = cached

    # ---- cache ---------------------------------------------------------

    def _read_cache(self) -> Weather | None:
        try:
            raw = json.loads(self.cache_path.read_text())
            if raw.get("lat") == self.lat and raw.get("lon") == self.lon:
                return Weather(**raw)
        except Exception:
            return None
        return None

    def _write_cache(self, w: Weather) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            data = {k: getattr(w, k) for k in w.__dataclass_fields__}
            fd, tmp = tempfile.mkstemp(dir=str(self.cache_path.parent))
            with open(fd, "w") as f:
                json.dump(data, f)
            Path(tmp).replace(self.cache_path)
        except Exception:
            pass

    # ---- fetch ---------------------------------------------------------

    def refresh(self, now: float | None = None, force: bool = False) -> Weather | None:
        """Fetch if stale; returns current Weather (possibly cached/stale)."""
        now = now or time.time()
        if not force and self.last is not None and now - self._last_fetch < self.refresh_s:
            return self.last
        self._last_fetch = now
        try:
            w = self._fetch(now)
            self.last = w
            self.last_error = None
            self._write_cache(w)
        except Exception as e:
            self.last_error = str(e)[:120]
            if self.last is not None:
                self.last.stale = True
        return self.last

    def _fetch(self, now: float) -> Weather:
        url = OPEN_METEO_URL.format(lat=self.lat, lon=self.lon)
        req = urllib.request.Request(url, headers={"User-Agent": "zionstats/0.2"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode())
        cur = data["current"]
        hourly = data.get("hourly", {})
        times, temps = hourly.get("time", []), hourly.get("temperature_2m", [])
        # hourly times are local ISO without offset; the API already resolved
        # the location's timezone -- anchor them to now for the sparkline.
        epoch0 = now
        points: list[tuple[float, float]] = []
        for i, t in enumerate(temps[:24]):
            points.append((epoch0 + i * 3600.0, float(t)))
        del times
        return Weather(
            ts=now, lat=self.lat, lon=self.lon, label=self.label,
            temp_c=float(cur["temperature_2m"]),
            rh=float(cur["relative_humidity_2m"]),
            dew_c=float(cur["dew_point_2m"]),
            apparent_c=float(cur["apparent_temperature"]),
            precip_mm=float(cur.get("precipitation", 0.0)),
            code=int(cur.get("weather_code", 0)),
            wind_kmh=float(cur.get("wind_speed_10m", 0.0)),
            is_day=bool(cur.get("is_day", 1)),
            hourly=points,
        )


class DemoWeather(WeatherProvider):
    """Tropical diurnal curve for --demo: 27-33C, 85-92% RH,
    afternoon drizzle bursts.  No network."""

    def __init__(self, lat: float = 0.0, lon: float = 0.0,
                 label: str = "demo location", refresh_min: float = 20.0) -> None:
        super().__init__(lat, lon, label, refresh_min, cache_path=Path("/dev/null"))
        self._t0 = time.time()

    def refresh(self, now: float | None = None, force: bool = False) -> Weather | None:
        now = now or time.time()
        self._last_fetch = now
        h = (now - self._t0) / 3600.0
        tod = (now % 86400) / 86400.0
        temp = 29.0 + 3.5 * math.sin((tod - 0.35) * 2 * math.pi) + 0.4 * math.sin(h * 7)
        rh = 88.0 - 8.0 * math.sin((tod - 0.35) * 2 * math.pi) + 1.5 * math.sin(h * 5)
        code = 51 if (tod > 0.55 and tod < 0.70) else (2 if 0.3 < tod < 0.55 else 1)
        w = Weather(
            ts=now, lat=self.lat, lon=self.lon, label=self.label,
            temp_c=round(temp, 1), rh=round(max(60.0, min(98.0, rh)), 0),
            dew_c=round(wet_bulb_c(temp, max(60.0, rh)) - 0.6, 1),
            apparent_c=round(temp + 4.0, 1),
            precip_mm=0.4 if code == 51 else 0.0,
            code=code, wind_kmh=8.0, is_day=0.25 < tod < 0.85,
            hourly=[(now + i * 3600.0, round(temp + math.sin(i / 4.0) * 1.8, 1)) for i in range(24)],
        )
        self.last = w
        self.last_error = None
        return w
