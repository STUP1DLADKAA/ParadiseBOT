"""Timezone resolution and time formatting utilities."""

from __future__ import annotations

import logging
import zoneinfo
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from rapidfuzz import fuzz, process

from functions.helpers import ParadiseError

log = logging.getLogger(__name__)

# Common timezone abbreviations to IANA mapping
TZ_ABBREVIATIONS: dict[str, str] = {
    "ist": "Asia/Kolkata",
    "gmt": "UTC",
    "utc": "UTC",
    "est": "America/New_York",
    "edt": "America/New_York",
    "cst": "America/Chicago",
    "cdt": "America/Chicago",
    "mst": "America/Denver",
    "mdt": "America/Denver",
    "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles",
    "akst": "America/Anchorage",
    "hst": "Pacific/Honolulu",
    "bst": "Europe/London",
    "wet": "Europe/Lisbon",
    "cet": "Europe/Paris",
    "cest": "Europe/Paris",
    "eet": "Europe/Athens",
    "eest": "Europe/Athens",
    "msk": "Europe/Moscow",
    "jst": "Asia/Tokyo",
    "kst": "Asia/Seoul",
    "cst_china": "Asia/Shanghai",
    "hkt": "Asia/Hong_Kong",
    "sgt": "Asia/Singapore",
    "ict": "Asia/Bangkok",
    "wib": "Asia/Jakarta",
    "wita": "Asia/Makassar",
    "wit": "Asia/Jayapura",
    "aest": "Australia/Sydney",
    "aedt": "Australia/Sydney",
    "acst": "Australia/Adelaide",
    "acdt": "Australia/Adelaide",
    "awst": "Australia/Perth",
    "nzst": "Pacific/Auckland",
    "nzdt": "Pacific/Auckland",
    "cat": "Africa/Harare",
    "eat": "Africa/Nairobi",
    "wat": "Africa/Lagos",
    "sast": "Africa/Johannesburg",
    "art": "America/Argentina/Buenos_Aires",
    "brt": "America/Sao_Paulo",
    "clt": "America/Santiago",
    "cot": "America/Bogota",
    "pet": "America/Lima",
    "vet": "America/Caracas",
    "gst": "Asia/Dubai",
    "ast": "Asia/Riyadh",
}

# City/Country names to IANA timezone
CITY_TIMEZONES: dict[str, str] = {
    "delhi": "Asia/Kolkata",
    "new delhi": "Asia/Kolkata",
    "mumbai": "Asia/Kolkata",
    "bangalore": "Asia/Kolkata",
    "bengaluru": "Asia/Kolkata",
    "kolkata": "Asia/Kolkata",
    "chennai": "Asia/Kolkata",
    "hyderabad": "Asia/Kolkata",
    "pune": "Asia/Kolkata",
    "india": "Asia/Kolkata",
    "london": "Europe/London",
    "uk": "Europe/London",
    "united kingdom": "Europe/London",
    "england": "Europe/London",
    "new york": "America/New_York",
    "nyc": "America/New_York",
    "los angeles": "America/Los_Angeles",
    "la": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles",
    "chicago": "America/Chicago",
    "seattle": "America/Los_Angeles",
    "miami": "America/New_York",
    "dallas": "America/Chicago",
    "houston": "America/Chicago",
    "boston": "America/New_York",
    "washington": "America/New_York",
    "toronto": "America/Toronto",
    "vancouver": "America/Vancouver",
    "montreal": "America/Toronto",
    "paris": "Europe/Paris",
    "france": "Europe/Paris",
    "berlin": "Europe/Berlin",
    "germany": "Europe/Berlin",
    "frankfurt": "Europe/Berlin",
    "tokyo": "Asia/Tokyo",
    "japan": "Asia/Tokyo",
    "osaka": "Asia/Tokyo",
    "kyoto": "Asia/Tokyo",
    "sydney": "Australia/Sydney",
    "melbourne": "Australia/Melbourne",
    "brisbane": "Australia/Brisbane",
    "perth": "Australia/Perth",
    "adelaide": "Australia/Adelaide",
    "australia": "Australia/Sydney",
    "singapore": "Asia/Singapore",
    "dubai": "Asia/Dubai",
    "uae": "Asia/Dubai",
    "abu dhabi": "Asia/Dubai",
    "hong kong": "Asia/Hong_Kong",
    "bangkok": "Asia/Bangkok",
    "thailand": "Asia/Bangkok",
    "seoul": "Asia/Seoul",
    "korea": "Asia/Seoul",
    "south korea": "Asia/Seoul",
    "rome": "Europe/Rome",
    "italy": "Europe/Rome",
    "milan": "Europe/Rome",
    "madrid": "Europe/Madrid",
    "spain": "Europe/Madrid",
    "barcelona": "Europe/Madrid",
    "amsterdam": "Europe/Amsterdam",
    "netherlands": "Europe/Amsterdam",
    "holland": "Europe/Amsterdam",
    "cairo": "Africa/Cairo",
    "egypt": "Africa/Cairo",
    "sao paulo": "America/Sao_Paulo",
    "brazil": "America/Sao_Paulo",
    "rio de janeiro": "America/Sao_Paulo",
    "buenos aires": "America/Argentina/Buenos_Aires",
    "argentina": "America/Argentina/Buenos_Aires",
    "mexico city": "America/Mexico_City",
    "mexico": "America/Mexico_City",
    "jakarta": "Asia/Jakarta",
    "indonesia": "Asia/Jakarta",
    "kuala lumpur": "Asia/Kuala_Lumpur",
    "malaysia": "Asia/Kuala_Lumpur",
    "auckland": "Pacific/Auckland",
    "new zealand": "Pacific/Auckland",
    "wellington": "Pacific/Auckland",
    "cape town": "Africa/Johannesburg",
    "johannesburg": "Africa/Johannesburg",
    "south africa": "Africa/Johannesburg",
    "zurich": "Europe/Zurich",
    "switzerland": "Europe/Zurich",
    "geneva": "Europe/Zurich",
    "stockholm": "Europe/Stockholm",
    "sweden": "Europe/Stockholm",
    "oslo": "Europe/Oslo",
    "norway": "Europe/Oslo",
    "helsinki": "Europe/Helsinki",
    "finland": "Europe/Helsinki",
    "copenhagen": "Europe/Copenhagen",
    "denmark": "Europe/Copenhagen",
    "vienna": "Europe/Vienna",
    "austria": "Europe/Vienna",
    "brussels": "Europe/Brussels",
    "belgium": "Europe/Brussels",
    "dublin": "Europe/Dublin",
    "ireland": "Europe/Dublin",
    "lisbon": "Europe/Lisbon",
    "portugal": "Europe/Lisbon",
    "athens": "Europe/Athens",
    "greece": "Europe/Athens",
    "istanbul": "Europe/Istanbul",
    "turkey": "Europe/Istanbul",
    "riyadh": "Asia/Riyadh",
    "saudi arabia": "Asia/Riyadh",
    "doha": "Asia/Qatar",
    "qatar": "Asia/Qatar",
    "moscow": "Europe/Moscow",
    "russia": "Europe/Moscow",
    "beijing": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai",
    "china": "Asia/Shanghai",
    "taipei": "Asia/Taipei",
    "taiwan": "Asia/Taipei",
    "manila": "Asia/Manila",
    "philippines": "Asia/Manila",
    "hanoi": "Asia/Ho_Chi_Minh",
    "vietnam": "Asia/Ho_Chi_Minh",
    "dhaka": "Asia/Dhaka",
    "bangladesh": "Asia/Dhaka",
    "kathmandu": "Asia/Kathmandu",
    "nepal": "Asia/Kathmandu",
    "colombo": "Asia/Colombo",
    "sri lanka": "Asia/Colombo",
    "karachi": "Asia/Karachi",
    "pakistan": "Asia/Karachi",
    "lahore": "Asia/Karachi",
    "islamabad": "Asia/Karachi",
    "warsaw": "Europe/Warsaw",
    "poland": "Europe/Warsaw",
    "prague": "Europe/Prague",
    "budapest": "Europe/Budapest",
    "bucharest": "Europe/Bucharest",
    "honolulu": "Pacific/Honolulu",
    "hawaii": "Pacific/Honolulu",
}


@dataclass(slots=True)
class TimeInfo:
    """Resolved time details for a location or user."""

    location_or_target: str
    tz_name: str
    now_dt: datetime
    formatted_12h: str
    formatted_24h: str
    formatted_date: str
    utc_offset: str
    epoch_timestamp: int
    abbreviation: str


def resolve_timezone(raw: str) -> str | None:
    """Resolve a raw string (city, abbreviation, or IANA name) to an IANA timezone string."""
    clean = raw.strip()
    if not clean:
        return None

    clean_lower = clean.lower()

    # 1. Check abbreviation map first (e.g. EST -> America/New_York, IST -> Asia/Kolkata)
    if clean_lower in TZ_ABBREVIATIONS:
        return TZ_ABBREVIATIONS[clean_lower]

    # 2. Check city / country map
    if clean_lower in CITY_TIMEZONES:
        return CITY_TIMEZONES[clean_lower]

    # 3. Match with available IANA timezones (e.g. Asia/Kolkata, Europe/Paris)
    available = zoneinfo.available_timezones()
    for tz in available:
        if tz.lower() == clean_lower:
            return tz

    # 4. Fuzzy match against city names and IANA timezones
    all_keys = list(CITY_TIMEZONES.keys()) + list(available)
    match = process.extractOne(
        clean_lower,
        all_keys,
        scorer=fuzz.token_sort_ratio,
        score_cutoff=65,
    )
    if match:
        matched_str = match[0]
        if matched_str in CITY_TIMEZONES:
            return CITY_TIMEZONES[matched_str]
        for tz in available:
            if tz.lower() == matched_str.lower():
                return tz

    return None


def get_current_time_for_zone(tz_str: str, label: str = "") -> TimeInfo:
    """Get rich TimeInfo for a resolved timezone string."""
    try:
        tz = zoneinfo.ZoneInfo(tz_str)
    except Exception as err:
        raise ParadiseError(f"Invalid timezone: `{tz_str}`") from err

    now = datetime.now(tz)
    offset = now.strftime("%z")  # e.g. +0530, -0400
    if len(offset) == 5:
        offset_fmt = f"UTC{offset[:3]}:{offset[3:]}"
    else:
        offset_fmt = f"UTC{offset}"

    tz_abbr = now.strftime("%Z") or tz_str

    return TimeInfo(
        location_or_target=label or tz_str,
        tz_name=tz_str,
        now_dt=now,
        formatted_12h=now.strftime("%I:%M:%S %p").lstrip("0"),
        formatted_24h=now.strftime("%H:%M:%S"),
        formatted_date=now.strftime("%A, %B %d, %Y"),
        utc_offset=offset_fmt,
        epoch_timestamp=int(now.timestamp()),
        abbreviation=tz_abbr,
    )
