"""The Response Formatter — clean, scannable, emoji-light cards.

One place decides what a title card, a trending list, an upcoming list
or a recommendation block looks like. The shape follows the system's
specification:

    🎬 Interstellar
    ⭐ Rating: …
    📅 Release: …
    🎭 Genre: …
    ⏱️ Runtime: …
    🎬 Director: …
    🌐 Status: …

Every rating names its source; unavailable facts are skipped (and noted)
rather than faked; unconfirmed things say so.
"""

from __future__ import annotations

import discord

from .models import (
    Identity,
    MediaKind,
    MediaTitle,
    RelatedTitle,
    ReleaseStatus,
    StreamingOffer,
    TrendingEntry,
    UpcomingEntry,
    kind_emoji,
    kind_label,
)

STATUS_EMOJI = {
    ReleaseStatus.UPCOMING: "🗓️",
    ReleaseStatus.AIRING: "📡",
    ReleaseStatus.RELEASED: "✅",
    ReleaseStatus.COMPLETED: "🏁",
    ReleaseStatus.CANCELLED: "🚫",
    ReleaseStatus.RENEWED: "🔁",
    ReleaseStatus.HIATUS: "⏸️",
    ReleaseStatus.UNKNOWN: "🌐",
}

STATUS_LABELS = {
    ReleaseStatus.UPCOMING: "Upcoming",
    ReleaseStatus.AIRING: "Airing now",
    ReleaseStatus.RELEASED: "Released",
    ReleaseStatus.COMPLETED: "Completed",
    ReleaseStatus.CANCELLED: "Cancelled",
    ReleaseStatus.RENEWED: "Renewed",
    ReleaseStatus.HIATUS: "On hiatus",
    ReleaseStatus.UNKNOWN: "Unknown",
}

_MAX_CARD = 3900  # embed description budget, with room to spare
_MAX_SYNOPSIS = 420


def esc(text: str) -> str:
    return discord.utils.escape_markdown(text or "")


def title_heading(title: MediaTitle) -> str:
    heading = f"{kind_emoji(title.kind)} {esc(title.title)}"
    if title.identity and title.identity.year:
        heading += f" ({title.identity.year})"
    return heading


def format_title_card(title: MediaTitle, *, detailed: bool = False) -> str:
    """The main card body — exactly the fields we could verify."""
    lines: list[str] = []

    if title.alt_titles:
        shown = [alt for alt in title.alt_titles[:2] if alt.casefold() != title.title.casefold()]
        if shown:
            lines.append(f"-# aka {esc(' · '.join(shown))}")

    # ⭐ ratings — always with their source named
    if title.ratings:
        bits = [rating.display() for rating in title.ratings[:3 if detailed else 2]]
        lines.append(f"⭐ **Rating:** {' · '.join(bits)}")
        if any(rating.via for rating in title.ratings) and not detailed:
            lines.append("-# ratings move around — live when fetched")

    # 📅 release
    release = title.release or title.upcoming
    if release and release.value:
        label = "Upcoming" if title.is_upcoming else "Release"
        date_text = release.human()
        if release.note:
            date_text += f" ({release.note})"
        lines.append(f"📅 **{label}:** {esc(date_text)}")

    # 🎭 genres
    if title.genres:
        lines.append(f"🎭 **Genre:** {esc(title.genre_line())}")

    # ⏱️ runtime / 🎞️ episodes
    if title.kind.is_series or title.kind.is_anime:
        episode_bits = []
        if title.seasons:
            episode_bits.append(f"{title.seasons} season{'s' if title.seasons != 1 else ''}")
        if title.episodes:
            episode_bits.append(f"{title.episodes} episodes")
        if title.episode_runtime:
            episode_bits.append(f"~{title.episode_runtime}m per episode")
        if episode_bits:
            lines.append(f"🎞️ **Episodes:** {', '.join(episode_bits)}")
        elif title.runtime_minutes:
            lines.append(f"⏱️ **Runtime:** {title.runtime_text()} per episode")
    elif title.runtime_minutes:
        lines.append(f"⏱️ **Runtime:** {title.runtime_text()}")

    # 🎬 people
    if title.creators:
        role = "Director" if title.kind in (MediaKind.MOVIE, MediaKind.ANIME_MOVIE, MediaKind.SHORT) else "Creator"
        lines.append(f"🎬 **{role}:** {esc(', '.join(title.creators[:3]))}")
    if title.cast:
        names = title.cast_names(4 if not detailed else 6)
        extra = len(title.cast) - len(names)
        suffix = f" (+{extra} more)" if extra > 0 else ""
        lines.append(f"🌟 **Cast:** {esc(', '.join(names))}{suffix}")

    # 🏭 studio / network
    if title.studios:
        label = "Studio" if title.kind.is_anime else "Production"
        lines.append(f"🏭 **{label}:** {esc(', '.join(title.studios[:3]))}")
    if title.networks:
        lines.append(f"📺 **Network:** {esc(', '.join(title.networks[:2]))}")

    # 🌐 country / language
    locale_bits = [bit for bit in (title.country, title.language) if bit]
    if locale_bits:
        lines.append(f"🌍 **Origin:** {esc(' · '.join(locale_bits[:2]))}")

    # status
    status = STATUS_LABELS.get(title.status, "Unknown")
    status_line = f"🌐 **Status:** {status}"
    if title.status_note:
        status_line += f" — {esc(title.status_note)}"
    lines.append(status_line)

    if title.popularity_hint:
        lines.append(f"🔥 **Buzz:** {esc(title.popularity_hint)}")

    # 📖 synopsis
    if title.synopsis:
        lines.append("")
        lines.append("**📖 Synopsis**")
        lines.append(esc(_clip(title.synopsis, _MAX_SYNOPSIS if not detailed else 900)))

    # 📡 where to watch
    streaming = format_streaming_block(title.streaming)
    if streaming:
        lines.append("")
        lines.append(streaming)

    # 🍿 similar / related
    if title.similar:
        lines.append("")
        lines.append("**🍿 You might also like**")
        for pick in title.similar[:4 if not detailed else 8]:
            lines.append(_related_line(pick))

    # 🎬 trailer
    trailer_bits = []
    if title.trailer_url:
        trailer_bits.append(f"[Watch the trailer]({title.trailer_url})")
    elif title.title:
        trailer_bits.append(
            f"[Search YouTube for the trailer](https://www.youtube.com/results?search_query={quote_plus(title.title + ' official trailer')})"
        )
    if title.official_site:
        trailer_bits.append(f"[Official site]({title.official_site})")
    if title.identity and title.identity.imdb_id:
        trailer_bits.append(f"[IMDb](https://www.imdb.com/title/{title.identity.imdb_id}/)")
    if trailer_bits:
        lines.append("")
        lines.append("🔗 " + " · ".join(trailer_bits))

    # honesty notes
    if title.notes:
        lines.append("")
        for note in title.notes[:4 if not detailed else 8]:
            prefix = "⚠️ " if note.unconfirmed else ""
            lines.append(f"-# {prefix}{esc(note.text)}")

    body = "\n".join(lines)
    return _clip(body, _MAX_CARD)


def format_streaming_block(offers: list[StreamingOffer]) -> str:
    if not offers:
        return ""
    bits: list[str] = []
    for offer in offers[:5]:
        bit = f"**{esc(offer.platform)}**"
        if offer.region:
            bit += f" ({esc(offer.region)})"
        if offer.url:
            bit = f"[{bit}]({offer.url})"
        bits.append(bit)
    sources = {offer.source for offer in offers}
    return "📡 **Where to watch:** " + " · ".join(bits) + _source_footnote(sources)


def format_streaming_reply(
    identity: Identity, offers: list[StreamingOffer], notes: list[str]
) -> str:
    """The dedicated where-to-watch answer."""
    lines = [f"📡 **Where to watch {esc(identity.title)}**", ""]
    if offers:
        for offer in offers[:8]:
            bit = f"• **{esc(offer.platform)}**"
            if offer.region:
                bit += f" — {esc(offer.region)}"
            if offer.source:
                bit += f" *({esc(offer.source)})*"
            if offer.url:
                bit = f"{bit} — [link]({offer.url})"
            lines.append(bit)
    else:
        lines.append(
            "couldn't verify any streaming platforms from live sources right now — "
            "availability changes by region and date, so it may genuinely be nowhere yet."
        )
    if notes:
        lines.append("")
        for note in notes[:4]:
            lines.append(f"-# {esc(note)}")
    return _clip("\n".join(lines), _MAX_CARD)


def format_trending(entries: list[TrendingEntry], notes: list[str], heading: str) -> str:
    lines = [f"🔥 **{heading}**", ""]
    for index, entry in enumerate(entries, 1):
        bit = f"{index}. **{esc(entry.headline())}**"
        label = {
            "trending": "🔥 trending",
            "popular": "👍 popular",
            "top rated": "⭐ top rated",
        }.get(entry.list_kind, "")
        if label:
            bit += f" · {label}"
        if entry.metric:
            bit += f"\n   -# {esc(entry.metric)}"
        lines.append(bit)
    if not entries:
        lines.append("couldn't verify any live trend data right now — please try again in a bit.")
    if notes:
        lines.append("")
        lines.append("-# " + esc(" · ".join(notes)))
    return _clip("\n".join(lines), _MAX_CARD)


def format_upcoming(entries: list[UpcomingEntry], notes: list[str], heading: str) -> str:
    lines = [f"🗓️ **{heading}**", ""]
    for entry in entries:
        bit = f"• **{esc(entry.title)}** — {esc(entry.when_text())}"
        extras = []
        if entry.status_note:
            extras.append(entry.status_note)
        if entry.platform:
            extras.append(f"on {entry.platform}")
        if entry.genres:
            extras.append(", ".join(entry.genres[:2]))
        if entry.kind is not MediaKind.UNKNOWN:
            extras.append(kind_label(entry.kind))
        if extras:
            bit += f" ({esc(' · '.join(extras))})"
        lines.append(bit)
    if not entries:
        lines.append("couldn't verify any upcoming releases for that window right now.")
    if notes:
        lines.append("")
        lines.append("-# " + esc(" · ".join(notes)))
    return _clip("\n".join(lines), _MAX_CARD)


def format_recommendations(
    picks: list[RelatedTitle], heading: str, intro: str = ""
) -> str:
    lines = [f"🍿 **{heading}**"]
    if intro:
        lines.append("")
        lines.append(esc(intro))
    lines.append("")
    for index, pick in enumerate(picks, 1):
        bit = f"{index}. **{esc(pick.headline())}**"
        if pick.rating:
            bit += f" — {pick.rating.display()}"
        if pick.why:
            bit += f"\n   -# why: {esc(pick.why)}"
        lines.append(bit)
    if not picks:
        lines.append("couldn't find solid recommendations right now — try naming a title you liked.")
    return _clip("\n".join(lines), _MAX_CARD)


def format_ambiguous(query: str, candidates: list[Identity]) -> str:
    lines = [f"🔍 I found multiple results for “{esc(query)}” — which one did you mean?"]
    return "\n".join(lines)


def loading_line(query: str) -> str:
    return f"🔎 searching the live web for “{esc(query)}”…"


def _related_line(pick: RelatedTitle) -> str:
    bit = f"• **{esc(pick.headline())}**"
    if pick.relation and pick.relation not in ("similar", "related"):
        bit += f" *({pick.relation})*"
    if pick.why:
        bit += f" — {esc(pick.why)}"
    elif pick.rating:
        bit += f" — {pick.rating.display()}"
    return bit


def _source_footnote(sources) -> str:
    if not sources:
        return ""
    return f"\n-# verified via {esc(', '.join(sorted(sources)))} — availability changes by region"


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def quote_plus(text: str) -> str:
    from urllib.parse import quote_plus as _qp

    return _qp(text)
