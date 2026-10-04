"""The Verification Layer: cross-check facts between sources.

Important information (release dates, runtimes, episode counts) is
collected from several independent databases. This module decides:

- which value to show when sources agree (any of them — they agree);
- which value to show when they disagree (the more specific/official
  one, with an honest note about the conflict);
- what counts as a conflict at all (a 2014 premiere vs a 2014 release
  within a year is normal; 2013 vs 2016 is not).

Everything here is pure — no network, no Discord.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import MediaTitle, Rating


@dataclass(slots=True)
class Observation:
    """One source's opinion about one fact."""

    source: str
    value: str


@dataclass(slots=True)
class Conflict:
    fact: str
    observations: list[Observation] = field(default_factory=list)
    chosen: str = ""
    note: str = ""


def check_years(observations: list[Observation]) -> Conflict | None:
    """Years more than 1 apart across sources are worth a footnote."""
    years: list[tuple[str, int]] = []
    for observation in observations:
        digits = "".join(ch for ch in observation.value if ch.isdigit())
        if len(digits) == 4:
            try:
                years.append((observation.source, int(digits)))
            except ValueError:
                continue
    if len(years) < 2:
        return None
    values = [year for _, year in years]
    if max(values) - min(values) <= 1:
        return None
    shown = min(years, key=lambda pair: pair[1])
    conflict = Conflict(fact="release year")
    conflict.observations = [
        Observation(source=source, value=str(year)) for source, year in years
    ]
    conflict.chosen = str(shown[1])
    conflict.note = (
        "release year differs across sources ("
        + ", ".join(f"{source}: {year}" for source, year in years)
        + ") — showing the earliest reported"
    )
    return conflict


def check_runtimes(observations: list[Observation], tolerance: float = 0.25) -> Conflict | None:
    """Runtimes more than ``tolerance`` apart get flagged."""
    runtimes: list[tuple[str, int]] = []
    for observation in observations:
        digits = "".join(ch for ch in observation.value.split()[0] if ch.isdigit())
        if digits:
            try:
                minutes = int(digits)
                if 5 <= minutes <= 600:
                    runtimes.append((observation.source, minutes))
            except ValueError:
                continue
    if len(runtimes) < 2:
        return None
    values = [minutes for _, minutes in runtimes]
    spread = max(values) - min(values)
    if spread <= max(2, tolerance * max(values)):
        return None
    conflict = Conflict(fact="runtime")
    conflict.observations = [
        Observation(source=source, value=f"{minutes} min") for source, minutes in runtimes
    ]
    conflict.chosen = str(max(values))  # databases usually round down
    conflict.note = (
        "runtime differs across sources ("
        + ", ".join(f"{source}: {minutes}m" for source, minutes in runtimes)
        + ")"
    )
    return conflict


def verify_title(title: MediaTitle) -> list[str]:
    """Run all cross-checks on an assembled title; returns note strings."""
    notes: list[str] = []

    year_observations = [
        Observation(source=source, value=str(year))
        for source, year in _year_observations(title)
    ]
    year_conflict = check_years(year_observations)
    if year_conflict:
        notes.append(year_conflict.note)

    runtime_observations = [
        Observation(source=source, value=str(minutes))
        for source, minutes in _runtime_observations(title)
    ]
    runtime_conflict = check_runtimes(runtime_observations)
    if runtime_conflict:
        notes.append(runtime_conflict.note)

    return notes


def _year_observations(title: MediaTitle) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    if title.identity and title.identity.year:
        out.append((title.identity.source or "identity", title.identity.year))
    if title.release and title.release.value:
        out.append((title.release.source or "release", title.release.value.year))
    seen: set[int] = set()
    result = []
    for source, year in out:
        if year in seen:
            continue
        seen.add(year)
        result.append((source, year))
    return result


def _runtime_observations(title: MediaTitle) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    if title.runtime_minutes:
        out.append(("database", title.runtime_minutes))
    if title.episode_runtime and title.episode_runtime != title.runtime_minutes:
        out.append(("per-episode", title.episode_runtime))
    return out


def dedupe_ratings(ratings: list[Rating]) -> list[Rating]:
    """One rating per named source (first/most authoritative wins)."""
    seen: set[str] = set()
    out: list[Rating] = []
    for rating in ratings:
        key = rating.source.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(rating)
    return out


def rating_sort_key(rating: Rating) -> tuple[int, float]:
    """Display order: direct database ratings first, then scraped ones."""
    direct = 0 if not rating.via else 1
    return (direct, -rating.normalized)
