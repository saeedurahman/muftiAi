"""Map CLI source names to scraper instances."""

from __future__ import annotations

from mufti_scraper.config import ScraperConfig
from mufti_scraper.sources.alikhlas import AlIkhlasSource
from mufti_scraper.sources.almufti import AlMuftiSource
from mufti_scraper.sources.banuri import BanuriSource
from mufti_scraper.sources.deoband import DeobandSource
from mufti_scraper.sources.karachi import KarachiSource


def get_sources(names: list[str], config: ScraperConfig):
    """Return list of source instances for given short names."""
    reg = {
        "banuri": BanuriSource(),
        "almuftionline": AlMuftiSource(),
        "deoband": DeobandSource(),
        "karachi": KarachiSource(max_depth=config.karachi_max_depth),
        "alikhlas": AlIkhlasSource(),
    }
    out = []
    for n in names:
        key = n.strip().lower()
        if key not in reg:
            raise ValueError(f"Unknown source: {n}. Known: {', '.join(sorted(reg))}")
        out.append(reg[key])
    return out


def all_source_names() -> list[str]:
    return sorted(["banuri", "almuftionline", "deoband", "karachi", "alikhlas"])
