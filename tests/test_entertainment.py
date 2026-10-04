"""Offline tests for the entertainment discovery system.

Every fixture in here is real payload data captured from the key-less
public APIs (IMDb suggestions, TVMaze, Kitsu, Wikipedia, the Wikimedia
pageviews service) — trimmed for size but structurally intact — so the
parsers are tested against exactly what the live services send.
"""

from __future__ import annotations

import os
import sys
import unittest
import types
from dataclasses import dataclass, field
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions.entertainment import nlu
from functions.entertainment.autocomplete import AutocompleteEngine, _score
from functions.entertainment.cache import MediaCache, TitleMemory, TTLCache
from functions.entertainment.details import DetailsBuilder
from functions.entertainment.format import (
    format_recommendations,
    format_title_card,
    format_trending,
    format_upcoming,
    title_heading,
)
from functions.entertainment.http import HttpError, NotFoundError
from functions.entertainment.models import (
    DateValue,
    Identity,
    MediaKind,
    MediaTitle,
    Rating,
    ReleaseStatus,
    RelatedTitle,
    StreamingOffer,
    TrendingEntry,
    UpcomingEntry,
)
from functions.entertainment.nlu import match_entertainment_request, parse_recommend_query
from functions.entertainment.resolver import Resolver
from functions.entertainment.service import (
    AMBIGUOUS,
    FOUND,
    NOT_FOUND,
    EntertainmentService,
)
from functions.entertainment.sources.anilist import AniListSource, identity_from_media
from functions.entertainment.sources.imdb import ImdbSource, parse_imdb_jsonld
from functions.entertainment.sources.kitsu import KitsuSource, entry_facts, identity_from_entry
from functions.entertainment.sources.tvmaze import TvMazeSource, show_facts
from functions.entertainment.sources.websearch import WebSearchSource
from functions.entertainment.sources.wikipedia import (
    WikipediaSource,
    article_is_media,
    display_title,
    kind_from_article,
)
from functions.entertainment.sources.wikidata import parse_sparql_date
from functions.entertainment.textutil import (
    parse_film_date,
    parse_infobox,
    parse_runtime,
    similarity,
    split_season_suffix,
    strip_html,
    title_match_score,
    wikitext_to_text,
)
from functions.entertainment.verify import check_years, dedupe_ratings, verify_title
from functions.helpers import ParadiseError
from functions.search.models import SearchResponse, SearchResult


# ===========================================================================
# Real fixtures (captured live, trimmed)
# ===========================================================================

IMDB_SUGGEST_NARUTO = {
    "d": [
        {"i": {"height": 1000, "imageUrl": "https://m.media-amazon.com/x1.jpg", "width": 680},
         "id": "tt0409591", "l": "Naruto", "q": "TV series", "qid": "tvSeries", "rank": 902,
         "s": "Junko Takeuchi, Maile Flanagan", "tl": "2002-2007 TV Series", "y": 2002, "yr": "2002-2007"},
        {"i": {"height": 1426, "imageUrl": "https://m.media-amazon.com/x2.jpg", "width": 1000},
         "id": "tt0988824", "l": "Naruto: Shippuden", "q": "TV series", "qid": "tvSeries", "rank": 689,
         "s": "Alexandre Crépet, Junko Takeuchi", "tl": "2007-2017 TV Series", "y": 2007, "yr": "2007-2017"},
        {"i": {"height": 480, "imageUrl": "https://m.media-amazon.com/x3.jpg", "width": 640},
         "id": "in0000258", "l": "Naruto", "rank": 159, "s": "Franchise"},
        {"i": {"height": 734, "imageUrl": "https://m.media-amazon.com/x4.jpg", "width": 640},
         "id": "tt6342474", "l": "Boruto: Naruto Next Generations", "q": "TV series", "qid": "tvSeries",
         "rank": 5445, "s": "Yuko Sanpei, Bennett Abara", "tl": "2017-2023 TV Series", "y": 2017, "yr": "2017-2023"},
        {"i": {"height": 734, "imageUrl": "https://m.media-amazon.com/x5.jpg", "width": 490},
         "id": "tt3717532", "l": "The Last: Naruto the Movie", "q": "feature", "qid": "movie",
         "rank": 12364, "s": "Junko Takeuchi, Nana Mizuki", "tl": "2014", "y": 2014},
        {"id": "nm1234567", "l": "Naruto Uzumaki", "qid": "name", "rank": 8000, "s": "Character"},
        {"id": "/toronto/", "l": "Toronto Int'l Film Festival", "s": "Discover the movies"},
    ],
    "q": "naruto", "v": 1,
}

IMDB_SUGGEST_ONEP = {
    "d": [
        {"i": {"height": 1407, "imageUrl": "https://m.media-amazon.com/op1.jpg", "width": 1024},
         "id": "tt0388629", "l": "One Piece", "q": "TV series", "qid": "tvSeries", "rank": 136,
         "s": "Mayumi Tanaka, Akemi Okamura", "tl": "1999 TV Series", "y": 1999},
        {"i": {"height": 2222, "imageUrl": "https://m.media-amazon.com/op2.jpg", "width": 1500},
         "id": "tt11737520", "l": "One Piece", "q": "TV series", "qid": "tvSeries", "rank": 637,
         "s": "Iñaki Godoy, Emily Rudd", "tl": "2023 TV Series", "y": 2023},
        {"i": {"height": 2552, "imageUrl": "https://m.media-amazon.com/op3.jpg", "width": 1728},
         "id": "tt1160419", "l": "Dune", "q": "feature", "qid": "movie", "rank": 356,
         "s": "Timothée Chalamet, Rebecca Ferguson", "tl": "2021", "y": 2021},
        {"i": {"height": 3000, "imageUrl": "https://m.media-amazon.com/op4.jpg", "width": 2000},
         "id": "in0000251", "l": "One Piece", "rank": 171, "s": "Franchise"},
        {"i": {"height": 3000, "imageUrl": "https://m.media-amazon.com/op5.jpg", "width": 2000},
         "id": "tt4508902", "l": "One Punch Man", "q": "TV series", "qid": "tvSeries", "rank": 1577,
         "s": "Makoto Furukawa, Kaito Ishikawa", "tl": "2015 TV Series", "y": 2015},
        {"i": {"height": 3648, "imageUrl": "https://m.media-amazon.com/op6.jpg", "width": 5472},
         "id": "tt1677720", "l": "Ready Player One", "q": "feature", "qid": "movie", "rank": 1097,
         "s": "Tye Sheridan, Olivia Cooke", "tl": "2018", "y": 2018},
        {"i": {"height": 1170, "imageUrl": "https://m.media-amazon.com/op7.jpg", "width": 780},
         "id": "tt0814243", "l": "One Piece: The Movie", "q": "feature", "qid": "movie", "rank": 21384,
         "s": "Akemi Okamura, Kappei Yamaguchi", "tl": "2000", "y": 2000},
        {"i": {"height": 5603, "imageUrl": "https://m.media-amazon.com/op8.jpg", "width": 3949},
         "id": "tt16183464", "l": "One Piece Film: Red", "q": "feature", "qid": "movie", "rank": 21707,
         "s": "Mayumi Tanaka, Kazuya Nakai", "tl": "2022", "y": 2022},
    ],
    "q": "one%20p", "v": 1,
}

IMDB_SUGGEST_INTERSTELLAR = {
    "d": [
        {"i": {"height": 3600, "imageUrl": "https://m.media-amazon.com/int1.jpg", "width": 2430},
         "id": "tt0816692", "l": "Interstellar", "q": "feature", "qid": "movie", "rank": 190,
         "s": "Matthew McConaughey, Anne Hathaway", "tl": "2014", "y": 2014},
        {"i": {"height": 1000, "imageUrl": "https://m.media-amazon.com/int2.jpg", "width": 675},
         "id": "tt3960394", "l": "Interview with the Vampire", "q": "TV series", "qid": "tvSeries",
         "rank": 939, "s": "Jacob Anderson, Sam Reid", "tl": "2022 TV Series", "y": 2022},
        {"i": {"height": 1320, "imageUrl": "https://m.media-amazon.com/int3.jpg", "width": 941},
         "id": "tt0165832", "l": "Interstate 60", "q": "feature", "qid": "movie", "rank": 4426,
         "s": "James Marsden, Gary Oldman", "tl": "2002", "y": 2002},
    ],
    "q": "inter", "v": 1,
}

IMDB_SUGGEST_IT = {
    "d": [
        {"i": {"imageUrl": "https://m.media-amazon.com/it1.jpg"}, "id": "tt1396484",
         "l": "It", "q": "feature", "qid": "movie", "rank": 531, "s": "Bill Skarsgård", "y": 2017},
        {"i": {"imageUrl": "https://m.media-amazon.com/it2.jpg"}, "id": "tt7364330",
         "l": "It Chapter Two", "q": "feature", "qid": "movie", "rank": 1109, "s": "James McAvoy", "y": 2019},
        {"i": {"imageUrl": "https://m.media-amazon.com/it3.jpg"}, "id": "tt21772332",
         "l": "It: Welcome to Derry", "q": "TV series", "qid": "tvSeries", "rank": 1800, "y": 2025},
    ],
    "q": "it", "v": 1,
}

IMDB_TITLE_PAGE_NARUTO = (
    '<script type="application/ld+json">'
    '{"@type":"TVSeries","name":"Naruto","datePublished":"2002-10-03",'
    '"aggregateRating":{"@type":"AggregateRating","ratingValue":"8.4","ratingCount":"46,300",'
    '"bestRating":"10","worstRating":"1"},"contentRating":"TV-14","genre":["Animation","Action","Adventure"]}'
    "</script>"
)

IMDB_TITLE_PAGE_INTERSTELLAR = (
    '<script type="application/ld+json">'
    '{"@type":"Movie","name":"Interstellar","datePublished":"2014-11-07",'
    '"aggregateRating":{"@type":"AggregateRating","ratingValue":"8.7","ratingCount":"2,100,000",'
    '"bestRating":"10","worstRating":"1"},"contentRating":"PG-13","genre":["Adventure","Drama","Sci-Fi"]}'
    "</script>"
)

TVMAZE_SHOW_NARUTO = {
    "id": 3397, "url": "https://www.tvmaze.com/shows/3397/naruto",
    "name": "Naruto", "type": "Animation", "language": "Japanese",
    "genres": ["Action", "Adventure", "Anime", "Fantasy"],
    "status": "Ended", "runtime": 23, "averageRuntime": 23,
    "premiered": "2002-10-03", "ended": "2007-02-08",
    "officialSite": None,
    "schedule": {"time": "19:30", "days": ["Wednesday"]},
    "rating": {"average": 8.4}, "weight": 88,
    "network": {"id": 118, "name": "TV Tokyo", "country": {"name": "Japan", "code": "JP", "timezone": "Asia/Tokyo"}},
    "webChannel": None, "dvdCountry": None,
    "externals": {"tvrage": 4759, "thetvdb": 78857, "imdb": "tt0409591"},
    "image": {"medium": "https://static.tvmaze.com/naruto-m.jpg",
              "original": "https://static.tvmaze.com/naruto.jpg"},
    "summary": "<p>Naruto Uzumaki, a hyperactive and quirky young ninja, dreams of becoming his village's leader.</p>",
    "updated": 1769198028,
    "_links": {"self": {"href": "https://api.tvmaze.com/shows/3397"}},
}

TVMAZE_SEARCH_NARUTO = [{"score": 24.91, "show": TVMAZE_SHOW_NARUTO}]

TVMAZE_SEASONS_NARUTO = [
    {"id": 1, "number": season, "episodeOrder": order, "premiereDate": "2002-10-03"}
    for season, order in ((1, 26), (2, 43), (3, 29), (4, 24), (5, 98))
]

TVMAZE_SHOW_SQUID = {
    "id": 43192, "url": "https://www.tvmaze.com/shows/43192/squid-game",
    "name": "Squid Game", "type": "Scripted", "language": "Korean",
    "genres": ["Action", "Drama", "Mystery", "Thriller"],
    "status": "Running", "runtime": 55, "averageRuntime": 54,
    "premiered": "2021-09-17", "ended": None, "officialSite": "https://www.netflix.com/title/81040344",
    "schedule": {"time": "", "days": []}, "rating": {"average": 8.1}, "weight": 98,
    "network": None,
    "webChannel": {"id": 3, "name": "Netflix",
                   "country": {"name": "United States", "code": "US", "timezone": "America/New_York"}},
    "dvdCountry": None, "externals": {"tvrage": None, "thetvdb": 362654, "imdb": "tt10919420"},
    "image": {"medium": "https://static.tvmaze.com/squid-m.jpg",
              "original": "https://static.tvmaze.com/squid.jpg"},
    "summary": "<p>Hundreds of cash-strapped players accept a strange invitation to compete in children's games.</p>",
    "updated": 1769198028, "_links": {"self": {"href": "https://api.tvmaze.com/shows/43192"}},
}

KITSU_SEARCH_NARUTO = {
    "data": [{
        "id": "11", "type": "anime",
        "attributes": {
            "slug": "naruto",
            "synopsis": "A millennium ago, the Nine-Tailed Fox, a demon, was revived. Naruto Uzumaki is a young ninja.",
            "titles": {"en": "Naruto", "en_jp": "Naruto", "ja_jp": "NARUTO"},
            "canonicalTitle": "Naruto",
            "abbreviatedTitles": [],
            "averageRating": "80.62",
            "ratingFrequencies": {},
            "userCount": 108000,
            "favoritesCount": 5000,
            "startDate": "2002-10-03", "endDate": "2007-02-08",
            "popularityRank": 91, "ratingRank": 497,
            "ageRating": "PG", "ageRatingGuide": "",
            "subtype": "TV", "status": "finished", "tba": None,
            "posterImage": {"large": "https://media.kitsu.app/naruto.jpg"},
            "episodeCount": 220, "episodeLength": 23, "totalLength": 5060,
            "youtubeVideoId": "-G9BqkgZXRA",
            "showType": "TV",
        },
    }]
}

ANILIST_SEARCH_NARUTO = {
    "data": {
        "Page": {
            "media": [{
                "id": 20, "idMal": 20,
                "title": {"romaji": "Naruto", "english": "Naruto", "native": "NARUTO"},
                "synonyms": [],
                "format": "TV", "status": "FINISHED", "episodes": 220, "duration": 23,
                "season": "FALL", "seasonYear": 2002,
                "startDate": {"year": 2002, "month": 10, "day": 3},
                "endDate": {"year": 2007, "month": 2, "day": 8},
                "genres": ["Action", "Adventure", "Fantasy"],
                "description": "A millennium ago, the Nine-Tailed Fox demon was revived.",
                "averageScore": 84, "popularity": 350000, "trending": 12, "favourites": 40000,
                "studios": {"nodes": [{"name": "Studio Pierrot"}]},
                "nextAiringEpisode": None,
                "externalLinks": [
                    {"site": "Crunchyroll", "url": "https://www.crunchyroll.com/naruto",
                     "type": "STREAMING", "language": "", "region": ""},
                    {"site": "Netflix", "url": "https://www.netflix.com/title/naruto",
                     "type": "STREAMING", "language": "", "region": "JP"},
                ],
                "trailer": {"id": "-G9BqkgZXRA", "site": "youtube"},
                "coverImage": {"extraLarge": "https://s4.anilist.co/naruto.jpg"},
                "rankings": [{"rank": 900, "type": "RATED", "context": "all_time", "allTime": True}],
                "relations": {
                    "edges": [
                        {"relationType": "SEQUEL", "node": {
                            "id": 1735, "type": "ANIME", "format": "TV",
                            "title": {"romaji": "Naruto: Shippuuden", "english": "Naruto: Shippuden"},
                            "startDate": {"year": 2007}}},
                        {"relationType": "SIDE_STORY", "node": {
                            "id": 8000, "type": "ANIME", "format": "MOVIE",
                            "title": {"romaji": "Naruto: Dai Katsugeki!!", "english": "Naruto the Movie: Guardians of the Crescent Moon Kingdom"},
                            "startDate": {"year": 2006}}},
                        {"relationType": "ADAPTATION", "node": {
                            "id": 1, "type": "MANGA",
                            "title": {"romaji": "Naruto", "english": "Naruto"},
                            "startDate": {"year": 1999}}},
                    ]
                },
                "recommendations": {
                    "nodes": [
                        {"rating": 120, "mediaPreview": {
                            "id": 21, "title": {"english": "One Piece", "romaji": "One Piece"},
                            "format": "TV", "averageScore": 87, "seasonYear": 1999}},
                        {"rating": 80, "mediaPreview": {
                            "id": 16498, "title": {"english": "Attack on Titan", "romaji": "Shingeki no Kyojin"},
                            "format": "TV", "averageScore": 85, "seasonYear": 2013}},
                    ]
                },
            }]
        }
    }
}

JIKAN_ANIME_FULL_NARUTO = {
    "mal_id": 20, "url": "https://myanimelist.net/anime/20/Naruto",
    "title": "Naruto", "title_english": "Naruto", "title_japanese": "NARUTO",
    "titles": [{"type": "Default", "title": "Naruto"}],
    "type": "TV", "source": "Manga", "episodes": 220,
    "status": "Finished Airing", "airing": False,
    "aired": {"from": "2002-10-03T00:00:00+00:00", "to": "2007-02-08T00:00:00+00:00"},
    "duration": "23 min per ep", "rating": "PG-13",
    "score": 8.0, "scored_by": 1500000, "rank": 570, "popularity": 9,
    "members": 2200000, "favorites": 80000,
    "synopsis": "A millennium ago, the Nine-Tailed Fox demon was revived.",
    "season": "fall", "year": 2002,
    "studios": [{"type": "anime", "name": "Studio Pierrot"}],
    "producers": [{"type": "producer", "name": "TV Tokyo"}],
    "licensors": [{"type": "licensor", "name": "Viz Media"}],
    "genres": [{"type": "anime", "name": "Action"}, {"type": "anime", "name": "Adventure"}],
    "themes": [],
    "streaming": [
        {"name": "Crunchyroll", "url": "https://www.crunchyroll.com/naruto"},
        {"name": "Netflix", "url": "https://www.netflix.com/title/naruto"},
    ],
    "relations": [
        {"relation": "Sequel", "entry": [
            {"mal_id": 1735, "type": "anime", "name": "Naruto: Shippuuden", "url": "https://myanimelist.net/anime/1735"}]},
        {"relation": "Adaptation", "entry": [
            {"mal_id": 15, "type": "manga", "name": "Naruto", "url": "https://myanimelist.net/manga/15"}]},
    ],
    "trailer": {"url": "https://www.youtube.com/watch?v=-G9BqkgZXRA"},
    "images": {"jpg": {"large_image_url": "https://cdn.myanimelist.net/naruto.jpg"}},
}

WIKI_SUMMARY_INTERSTELLAR = {
    "type": "standard", "title": "Interstellar (film)",
    "displaytitle": "Interstellar (film)", "wikibase_item": "Q13417189",
    "titles": {"canonical": "Interstellar_(film)"},
    "thumbnail": {"source": "https://upload.wikimedia.org/thumb.jpg", "width": 220, "height": 326},
    "originalimage": {"source": "https://upload.wikimedia.org/poster.jpg", "width": 220, "height": 326},
    "lang": "en", "dir": "ltr",
    "description": "2014 film by Christopher Nolan",
    "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Interstellar_(film)"}},
    "extract": (
        "Interstellar is a 2014 epic science fiction drama film directed by "
        "Christopher Nolan, who co-wrote the screenplay with his brother, Jonathan Nolan. "
        "It features an ensemble cast led by Matthew McConaughey, Anne Hathaway, Jessica Chastain. "
        "Set in a dystopian future, the film follows a group of astronauts who travel through "
        "space in search of a new home for humanity."
    ),
}

WIKI_WIKITEXT_INTERSTELLAR = (
    "{{Short description|2014 film by Christopher Nolan}}\n"
    "{{Infobox film\n"
    "| director       = [[Christopher Nolan]]\n"
    "| writer         = {{Plainlist|\n* [[Jonathan Nolan]]\n* Christopher Nolan\n}}\n"
    "| producer       = {{Plainlist|\n* [[Emma Thomas]]\n* Christopher Nolan\n}}\n"
    "| starring       = {{Plainlist| \n* [[Matthew McConaughey]]\n* [[Anne Hathaway]]\n"
    "* [[Jessica Chastain]]\n* [[Bill Irwin]]\n* [[Ellen Burstyn]]\n* [[Michael Caine]]\n\n}}\n"
    "| music          = [[Hans Zimmer]]\n"
    "| studio         = {{Plainlist|\n* [[Paramount Pictures]]\n* [[Warner Bros. Pictures]]\n"
    "* [[Legendary Pictures]]\n}}\n"
    "| distributor    = {{Plainlist|\n* Paramount Pictures (United States)\n"
    "* Warner Bros. Pictures (International)\n}}\n"
    "| released       = {{Film date|2014|10|26|[[TCL Chinese Theatre]]|2014|11|05|United States|2014|11|07|United Kingdom}}\n"
    "| runtime        = 169 minutes\n"
    "| country        = {{Plainlist|\n* United States\n* United Kingdom\n}}\n"
    "| language       = English\n"
    "| budget         = $165 million\n"
    "| gross          = $774.7 million\n"
    "}}\n"
    "'''''Interstellar''''' is a 2014 [[Epic film|epic]] science fiction drama film.\n"
    "==Reception==\n"
    "On review aggregator [[Rotten Tomatoes]], 75% of 364 reviews are positive, with an "
    "average rating of 7.4/10. On [[Metacritic]], the film has a weighted average score of 74 out of 100. "
    "Audiences polled by CinemaScore gave the film an average grade of \"B\".\n"
)

WIKI_SUMMARY_NARUTO_TV = {
    "type": "standard", "title": "Naruto (TV series)", "wikibase_item": "Q114015",
    "description": "Japanese anime television series",
    "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Naruto_(TV_series)"}},
    "thumbnail": {"source": "https://upload.wikimedia.org/naruto-thumb.jpg", "width": 220, "height": 124},
    "extract": (
        "Naruto is a Japanese anime television series based on Masashi Kishimoto's manga series. "
        "It follows Naruto Uzumaki, a young ninja who seeks recognition and dreams of becoming Hokage."
    ),
}

WIKI_WIKITEXT_NARUTO_TV = (
    "{{Infobox television\n"
    "| image = Naruto Logo.svg\n"
    "| genre = {{Plainlist|\n* [[Adventure fiction|Adventure]]\n* [[Fantasy]]\n* [[Martial arts]]\n}}\n"
    "| creator = [[Masashi Kishimoto]]\n"
    "| director = {{Plainlist|\n* [[Hayato Date]]\n}}\n"
    "| studio = [[Pierrot (company)|Pierrot]]\n"
    "| network = [[TV Tokyo]]\n"
    "| num_seasons = 5\n"
    "| num_episodes = 220\n"
    "| release_dates = {{Start date|2002|10|3}}\n"
    "| runtime = 23 minutes\n"
    "| country = Japan\n"
    "| language = Japanese\n"
    "}}\n"
)

WIKI_PAGEVIEWS_TOP = {
    "items": [{
        "project": "en.wikipedia", "access": "all-access",
        "year": "2026", "month": "10", "day": "02",
        "articles": [
            {"article": "Main_Page", "views": 7362810, "rank": 1},
            {"article": "Digger_(2026_film)", "views": 238832, "rank": 9},
            {"article": "Drishyam:_The_Conclusion", "views": 235494, "rank": 10},
            {"article": "Bella_Ramsey", "views": 211074, "rank": 11},
            {"article": "East_of_Eden_(2026_miniseries)", "views": 171814, "rank": 14},
            {"article": "Verity_(film)", "views": 151519, "rank": 18},
            {"article": "Deaths_in_2026", "views": 136072, "rank": 20},
            {"article": "Verity_(novel)", "views": 97738, "rank": 26},
            {"article": "The_Celebrity_Traitors_series_2", "views": 97669, "rank": 27},
            {"article": "American_Horror_Story:_13", "views": 58222, "rank": 52},
            {"article": "Resident_Evil_(2026_film)", "views": 54551, "rank": 57},
            {"article": "Lanterns_(TV_series)", "views": 42829, "rank": 91},
            {"article": "Spider-Man:_Brand_New_Day", "views": 32928, "rank": 127},
            {"article": "Naruto_(TV_series)", "views": 12345, "rank": 900},
            {"article": "List_of_highest-grossing_films", "views": 35769, "rank": 112},
        ],
    }]
}

WIKIDATA_UPCOMING = {
    "head": {"vars": ["item", "itemLabel", "date", "precision", "sitelinks", "genreLabel"]},
    "results": {"bindings": [
        {"item": {"type": "uri", "value": "http://www.wikidata.org/entity/Q1001"},
         "itemLabel": {"type": "literal", "value": "Dune: Part Three"},
         "date": {"datatype": "xsd:dateTime", "type": "literal", "value": "2026-12-18T00:00:00Z"},
         "precision": {"type": "literal", "value": "11"},
         "sitelinks": {"datatype": "xsd:integer", "type": "literal", "value": "120"},
         "genreLabel": {"type": "literal", "value": "science fiction film"}},
        {"item": {"type": "uri", "value": "http://www.wikidata.org/entity/Q1002"},
         "itemLabel": {"type": "literal", "value": "Supergirl"},
         "date": {"datatype": "xsd:dateTime", "type": "literal", "value": "2027-01-01T00:00:00Z"},
         "precision": {"type": "literal", "value": "9"},
         "sitelinks": {"datatype": "xsd:integer", "type": "literal", "value": "200"},
         "genreLabel": {"type": "literal", "value": "superhero film"}},
        {"item": {"type": "uri", "value": "http://www.wikidata.org/entity/Q1003"},
         "itemLabel": {"type": "literal", "value": "Q1003"},
         "date": {"datatype": "xsd:dateTime", "type": "literal", "value": "2026-11-20T00:00:00Z"},
         "precision": {"type": "literal", "value": "11"},
         "sitelinks": {"datatype": "xsd:integer", "type": "literal", "value": "80"}},
    ]}
}

ANILIST_TRENDING = {
    "data": {"Page": {"media": [{
        "id": 1535, "idMal": 1535,
        "title": {"romaji": "Death Note", "english": "Death Note", "native": "DEATH NOTE"},
        "format": "TV", "status": "FINISHED", "episodes": 37, "duration": 23,
        "startDate": {"year": 2006, "month": 10, "day": 3}, "endDate": {"year": 2007, "month": 6, "day": 26},
        "genres": ["Mystery", "Psychological", "Supernatural", "Thriller"],
        "description": "A shinigami notebook.", "averageScore": 86,
        "popularity": 800000, "trending": 55, "favourites": 200000,
        "studios": {"nodes": [{"name": "Madhouse"}]},
        "externalLinks": [], "trailer": None,
        "coverImage": {"extraLarge": "https://s4.anilist.co/dn.jpg"}, "rankings": [],
    }]}}
}

ITUNES_TOP_MOVIES = {
    "feed": {"title": "Top Movies", "results": [
        {"artistName": "Christopher Nolan", "id": "1001",
         "name": "Oppenheimer", "releaseDate": "2023-07-21", "url": "https://tv.apple.com/oppenheimer"},
        {"artistName": "Greta Gerwig", "id": "1002",
         "name": "Barbie", "releaseDate": "2023-07-21", "url": "https://tv.apple.com/barbie"},
    ]}
}

ITUNES_SEARCH_INTERSTELLAR = {
    "resultCount": 1,
    "results": [{
        "wrapperType": "track", "kind": "feature-movie",
        "trackName": "Interstellar", "artistName": "Christopher Nolan",
        "releaseDate": "2014-11-05T07:00:00Z", "primaryGenreName": "Sci-Fi & Fantasy",
        "longDescription": "A team of explorers travel through a wormhole in space.",
        "trackTimeMillis": 10140000, "artworkUrl100": "https://is1-ssl.mzstatic.com/interstellar100.jpg",
        "trackViewUrl": "https://tv.apple.com/interstellar", "contentAdvisoryRating": "PG-13",
    }]
}

TVMAZE_SCHEDULE_TODAY = [
    {
        "id": 1, "url": "https://www.tvmaze.com/episodes/1/x", "name": "Episode 1",
        "season": 1, "number": 1, "type": "regular",
        "airdate": date.today().isoformat(), "airtime": "03:00",
        "airstamp": f"{date.today().isoformat()}T03:00:00+00:00", "runtime": 50,
        "summary": None,
        "_embedded": {"show": {
            "id": 100, "url": "https://www.tvmaze.com/shows/100/wednesday",
            "name": "Wednesday", "type": "Scripted", "language": "English",
            "genres": ["Comedy", "Fantasy", "Mystery"], "status": "Running",
            "runtime": 50, "averageRuntime": 51, "premiered": "2022-11-23", "ended": None,
            "officialSite": "https://www.netflix.com/title/81161626",
            "schedule": {"time": "", "days": []}, "rating": {"average": 8.3}, "weight": 97,
            "network": None,
            "webChannel": {"id": 3, "name": "Netflix",
                           "country": {"name": "United States", "code": "US"}},
            "externals": {"imdb": "tt13443470"}, "image": {"original": "https://static.tvmaze.com/wed.jpg"},
            "summary": "<p>Wednesday Addams investigates a murder spree.</p>",
        }},
    },
    {
        "id": 2, "url": "https://www.tvmaze.com/episodes/2/x", "name": "Episode 4",
        "season": 2, "number": 4, "type": "regular",
        "airdate": date.today().isoformat(), "airtime": "02:00",
        "airstamp": f"{date.today().isoformat()}T02:00:00+00:00", "runtime": 24,
        "summary": None,
        "_embedded": {"show": {
            "id": 200, "url": "https://www.tvmaze.com/shows/200/low-weight",
            "name": "Some Obscure Anime", "type": "Animation", "language": "Japanese",
            "genres": ["Anime"], "status": "Running", "runtime": 24, "averageRuntime": 24,
            "premiered": "2026-10-01", "ended": None, "officialSite": None,
            "schedule": {"time": "", "days": []}, "rating": {"average": None}, "weight": 20,
            "network": None,
            "webChannel": {"id": 9, "name": "Crunchyroll",
                           "country": {"name": "United States", "code": "US"}},
            "externals": {"imdb": None}, "image": {"original": ""},
            "summary": None,
        }},
    },
]


# ===========================================================================
# Fake transport
# ===========================================================================


class FakeHttpClient:
    """Routes source requests to fixture data. Unrouted = network down."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, object]] = []

    def route(self, method: str, url_contains: str, handler) -> None:
        self.routes.append((method, url_contains.casefold(), handler))

    def _dispatch(self, method: str, url: str, params, body=None, text=False):
        folded = url.casefold()
        for route_method, needle, handler in self.routes:
            if route_method == method and needle in folded:
                result = handler(params or {}, url, body)
                if result is _NOT_FOUND:
                    raise NotFoundError(f"no fixture entry for {url}")
                if result is _NETWORK_DOWN:
                    raise HttpError("simulated network failure")
                if text and isinstance(result, (dict, list)):
                    import json

                    return json.dumps(result)
                return result
        raise HttpError(f"no fixture route for {url}")

    async def get_json(self, url, *, params=None, timeout=None, max_bytes=2**20):
        return self._dispatch("GET", url, params)

    async def get_text(self, url, *, params=None, timeout=None, max_bytes=2**20, headers=None):
        return self._dispatch("GET", url, params, text=True)

    async def post_json(self, url, payload, *, timeout=None, max_bytes=2**20):
        return self._dispatch("POST", url, {}, body=payload)

    async def close(self) -> None:
        pass


class _Sentinel:
    def __repr__(self):  # pragma: no cover
        return "<sentinel>"


_NOT_FOUND = _Sentinel()
_NETWORK_DOWN = _Sentinel()


@dataclass
class FakeSearchEngine:
    """Stands in for the repo's DDG-backed SearchEngine."""
    results: list = field(default_factory=list)

    async def search(self, query: str, limit: int = 8) -> SearchResponse:
        lowered = query.casefold()
        matches = [
            SearchResult(title=r["title"], url=r["url"], snippet=r.get("snippet", ""))
            for r in self.results
            if any(word in lowered for word in r.get("_keywords", [])) or not r.get("_keywords")
        ][:limit]
        return SearchResponse(query=query, results=matches, engine="Fake")

    async def close(self):
        pass


# ===========================================================================
# Fixture wiring
# ===========================================================================


def build_fake_client() -> FakeHttpClient:
    client = FakeHttpClient()

    # --- IMDb suggestions ---------------------------------------------------
    def imdb_suggest(params, body):
        return IMDB_SUGGEST_NARUTO  # overridden per-test below when needed
    client.route("GET", "media-imdb.com/suggestion", imdb_suggest)
    return client


def wire_naruto_world(client: FakeHttpClient, search: FakeSearchEngine) -> None:
    """A full, consistent fixture set around Naruto."""

    def suggest(params, url, body):
        return IMDB_SUGGEST_NARUTO
    client.routes = [r for r in client.routes if "media-imdb.com" not in r[1]]

    def suggest_router(params, url, body):
        return IMDB_SUGGEST_NARUTO
    client.route("GET", "media-imdb.com/suggestion", suggest_router)

    def imdb_page(params, url, body):
        return IMDB_TITLE_PAGE_NARUTO
    client.route("GET", "imdb.com/title/tt0409591", imdb_page)

    def tvmaze_lookup(params, url, body):
        if params.get("imdb") == "tt0409591":
            return TVMAZE_SHOW_NARUTO
        return _NOT_FOUND
    client.route("GET", "tvmaze.com/lookup", tvmaze_lookup)

    def tvmaze_search(params, url, body):
        return TVMAZE_SEARCH_NARUTO
    client.route("GET", "tvmaze.com/search/shows", tvmaze_search)

    def tvmaze_seasons(params, url, body):
        return TVMAZE_SEASONS_NARUTO
    client.route("GET", "tvmaze.com/shows/3397/seasons", tvmaze_seasons)

    def kitsu_anime(params, url, body):
        if params.get("filter[text]") is not None:
            return KITSU_SEARCH_NARUTO
        return {"data": []}
    client.route("GET", "kitsu.app/api/edge/anime", kitsu_anime)

    def anilist(params, url, body):
        query = (body or {}).get("query", "")
        if "TRENDING_DESC" in query:
            return ANILIST_TRENDING
        if "NOT_YET_RELEASED" in query:
            return {"data": {"Page": {"media": []}}}
        return ANILIST_SEARCH_NARUTO
    client.route("POST", "graphql.anilist.co", anilist)

    def jikan_full(params, url, body):
        return {"data": JIKAN_ANIME_FULL_NARUTO}
    client.route("GET", "jikan.moe/v4/anime/20", jikan_full)

    def wiki_summary(params, url, body):
        return _NOT_FOUND
    client.route("GET", "wikipedia.org/api/rest_v1/page/summary", wiki_summary)

    def wiki_action(params, url, body):
        if params.get("action") == "opensearch":
            return ["naruto", ["Naruto (TV series)"], [""], ["https://en.wikipedia.org/wiki/Naruto_(TV_series)"]]
        return {}
    client.route("GET", "wikipedia.org/w/api.php", wiki_action)

    def wikidata_sparql(params, url, body):
        return WIKIDATA_UPCOMING
    client.route("GET", "query.wikidata.org/sparql", wikidata_sparql)

    def itunes(params, url, body):
        if "rss.itunes.apple.com" in str(params):
            return ITUNES_TOP_MOVIES
        return ITUNES_SEARCH_INTERSTELLAR
    client.route("GET", "itunes.apple.com", itunes)

    def tvmaze_schedule(params, url, body):
        return TVMAZE_SCHEDULE_TODAY
    client.route("GET", "tvmaze.com/schedule", tvmaze_schedule)


def wire_interstellar_world(client: FakeHttpClient, search: FakeSearchEngine) -> None:
    """A consistent fixture set around Interstellar (the movie)."""

    def suggest(params, url, body):
        # The queried name is embedded in the URL path.
        known = {
            "inception": ("tt1375666", "Inception", "movie", 2010),
            "arrival": ("tt2543164", "Arrival", "movie", 2016),
            "the martian": ("tt3659388", "The Martian", "movie", 2015),
        }
        slug = url.rsplit("/", 1)[-1].removesuffix(".json").replace("%20", " ").replace("_", " ")
        for key, (imdb_id, label, qid, year) in known.items():
            if slug == key:
                return {"d": [{
                    "i": {"imageUrl": "https://m.media-amazon.com/v.jpg"},
                    "id": imdb_id, "l": label, "qid": qid, "rank": 300, "y": year,
                }]}
        return IMDB_SUGGEST_INTERSTELLAR

    client.routes = [r for r in client.routes if "media-imdb.com" not in r[1]]
    client.route("GET", "media-imdb.com/suggestion", suggest)

    def imdb_page(params, url, body):
        return IMDB_TITLE_PAGE_INTERSTELLAR
    client.route("GET", "imdb.com/title/tt0816692", imdb_page)

    def wiki_action(params, url, body):
        if params.get("action") == "opensearch":
            return ["interstellar", ["Interstellar (film)"], [""],
                    ["https://en.wikipedia.org/wiki/Interstellar_(film)"]]
        return {
            "batchcomplete": True,
            "query": {"pages": [{
                "pageid": 6009939, "ns": 0, "title": "Interstellar (film)",
                "revisions": [{"slots": {"main": {
                    "contentmodel": "wikitext", "contentformat": "text/x-wiki",
                    "content": WIKI_WIKITEXT_INTERSTELLAR,
                }}}],
            }]},
        }
    client.route("GET", "wikipedia.org/w/api.php", wiki_action)

    def kitsu(params, url, body):
        return {"data": []}
    client.route("GET", "kitsu.app/api/edge/anime", kitsu)

    def anilist(params, url, body):
        return {"data": {"Page": {"media": []}}}
    client.route("POST", "graphql.anilist.co", anilist)

    def itunes_search(params, url, body):
        return ITUNES_SEARCH_INTERSTELLAR
    client.route("GET", "itunes.apple.com/search", itunes_search)

    def wikidata_sparql(params, url, body):
        return WIKIDATA_UPCOMING
    client.route("GET", "query.wikidata.org/sparql", wikidata_sparql)


def add_wiki_summaries(client: FakeHttpClient, summaries: dict[str, dict]) -> None:
    """Route REST summaries by the slug embedded in the URL.

    The FakeHttpClient matches on URL substrings, so we register one route
    per slug — with the caveat that a slug route must match before the
    generic one. Routes are evaluated in order, so we insert at the front.
    """
    client.routes = [r for r in client.routes if "page/summary" not in r[1]]
    lowered = {key.casefold(): value for key, value in summaries.items()}

    def handler_for(slug_value):
        def handler(params, url, body):
            return lowered[slug_value]
        return handler

    for slug in lowered:
        client.routes.insert(0, ("GET", f"/{slug}", handler_for(slug)))

    def missing(params, url, body):
        return _NOT_FOUND
    client.route("GET", "page/summary", missing)


def make_service(client: FakeHttpClient, search: FakeSearchEngine) -> EntertainmentService:
    service = EntertainmentService(client=client)
    service.web.engine = search
    return service


# ===========================================================================
# Text utilities
# ===========================================================================


class TestTextUtil(unittest.TestCase):
    def test_strip_html(self):
        self.assertEqual(strip_html("<p>Hello <b>world</b></p>"), "Hello world")
        self.assertEqual(strip_html(None), "")
        self.assertEqual(strip_html("a &amp; b"), "a & b")

    def test_wikitext_to_text_flattens_lists(self):
        value = "{{Plainlist|\n* [[Matthew McConaughey]]\n* [[Anne Hathaway]]\n}}"
        self.assertEqual(
            wikitext_to_text(value), "Matthew McConaughey, Anne Hathaway"
        )

    def test_wikitext_to_text_handles_film_date(self):
        value = "{{Film date|2014|10|26|[[TCL Chinese Theatre]]|2014|11|05|United States}}"
        text = wikitext_to_text(value)
        self.assertIn("2014", text)

    def test_parse_film_date_first_triple(self):
        triple = parse_film_date("2014, 10, 26, TCL Chinese Theatre, 2014, 11, 05, United States")
        self.assertEqual(triple, (2014, 10, 26))

    def test_parse_film_date_none(self):
        self.assertIsNone(parse_film_date("TBA"))
        self.assertIsNone(parse_film_date(""))

    def test_parse_runtime(self):
        self.assertEqual(parse_runtime("169 minutes"), 169)
        self.assertEqual(parse_runtime("23 min per ep"), 23)
        self.assertEqual(parse_runtime("2h 30m"), 150)
        self.assertIsNone(parse_runtime("n/a"))

    def test_parse_infobox_real_interstellar(self):
        params = parse_infobox(WIKI_WIKITEXT_INTERSTELLAR, "Infobox film")
        self.assertEqual(params["director"], "Christopher Nolan")
        self.assertEqual(params["runtime"], "169 minutes")
        self.assertEqual(params["language"], "English")
        self.assertIn("Matthew McConaughey", params["starring"])
        self.assertIn("Paramount Pictures", params["studio"])
        self.assertIn("2014", params["released"])

    def test_parse_infobox_real_naruto_tv(self):
        params = parse_infobox(WIKI_WIKITEXT_NARUTO_TV, "Infobox television")
        self.assertEqual(params["num_seasons"], "5")
        self.assertEqual(params["num_episodes"], "220")
        self.assertEqual(params["network"], "TV Tokyo")
        self.assertEqual(params["creator"], "Masashi Kishimoto")

    def test_split_season_suffix(self):
        self.assertEqual(split_season_suffix("naruto season 2"), ("naruto", 2))
        self.assertEqual(split_season_suffix("Demo Slayer s3"), ("Demo Slayer", 3))
        self.assertEqual(split_season_suffix("attack on titan part 2"), ("attack on titan part 2", None))
        self.assertEqual(split_season_suffix("one piece"), ("one piece", None))

    def test_similarity_exact_and_fuzzy(self):
        self.assertEqual(similarity("Naruto", "naruto"), 100.0)
        self.assertGreater(similarity("naurto", "Naruto"), 80.0)
        self.assertLess(similarity("Naruto", "Interstellar"), 40.0)

    def test_title_match_score_with_alts(self):
        score = title_match_score("shingeki no kyojin", "Attack on Titan",
                                  ["Shingeki no Kyojin", "進撃の巨人"])
        self.assertEqual(score, 100.0)


# ===========================================================================
# Source parsers (against real payload shapes)
# ===========================================================================


class TestSourceParsers(unittest.TestCase):
    def test_imdb_suggest_parsing(self):
        client = FakeHttpClient()
        client.route("GET", "media-imdb.com/suggestion", lambda p, u, b: IMDB_SUGGEST_NARUTO)
        source = ImdbSource(client)

        import asyncio

        identities = asyncio.run(source.suggest("naruto"))
        self.assertTrue(identities)
        by_id = {i.imdb_id: i for i in identities if i.imdb_id}
        self.assertIn("tt0409591", by_id)
        naruto = by_id["tt0409591"]
        self.assertEqual(naruto.kind, MediaKind.TV_SERIES)
        self.assertEqual(naruto.year, 2002)
        self.assertEqual(naruto.year_end, 2007)
        self.assertEqual(by_id["tt3717532"].kind, MediaKind.MOVIE)
        self.assertEqual(naruto.popularity, 75.0)  # rank 902
        franchises = [i for i in identities if i.kind is MediaKind.FRANCHISE]
        self.assertEqual(len(franchises), 1)
        # Non-title ids are filtered out.
        self.assertNotIn("Naruto Uzumaki", by_id)
        self.assertFalse(any(i.title == "Toronto Int'l Film Festival" for i in identities))

    def test_imdb_suggest_onep_parsing(self):
        client = FakeHttpClient()
        client.route("GET", "media-imdb.com/suggestion", lambda p, u, b: IMDB_SUGGEST_ONEP)
        source = ImdbSource(client)

        import asyncio

        identities = asyncio.run(source.suggest("one p"))
        titles = [i.title for i in identities]
        self.assertIn("One Piece", titles)
        self.assertIn("One Piece Film: Red", titles)
        self.assertIn("One Punch Man", titles)
        franchise = [i for i in identities if i.kind is MediaKind.FRANCHISE]
        self.assertEqual(len(franchise), 1)

    def test_imdb_jsonld_rating(self):
        data = parse_imdb_jsonld(IMDB_TITLE_PAGE_INTERSTELLAR, "tt0816692")
        self.assertEqual(data["rating"], 8.7)
        self.assertEqual(data["votes"], 2100000)
        self.assertEqual(data["date"], "2014-11-07")

    def test_tvmaze_show_facts(self):
        facts = show_facts(TVMAZE_SHOW_NARUTO)
        self.assertEqual(facts["kind"], MediaKind.TV_SERIES)
        self.assertEqual(facts["status"], ReleaseStatus.COMPLETED)
        self.assertEqual(facts["runtime"], 23)
        self.assertIn("Anime", facts["genres"])
        self.assertEqual(facts["network"], "TV Tokyo")
        self.assertEqual(facts["rating"].value, 8.4)

    def test_tvmaze_web_series_detection(self):
        facts = show_facts(TVMAZE_SHOW_SQUID)
        self.assertEqual(facts["kind"], MediaKind.WEB_SERIES)
        self.assertEqual(facts["web_channel"], "Netflix")

    def test_kitsu_entry_facts(self):
        entry = KITSU_SEARCH_NARUTO["data"][0]["attributes"]
        identity = identity_from_entry(entry, "11")
        self.assertEqual(identity.title, "Naruto")
        self.assertEqual(identity.kind, MediaKind.ANIME)
        self.assertEqual(identity.year, 2002)
        facts = entry_facts(entry)
        self.assertEqual(facts["episodes"], 220)
        self.assertEqual(facts["episode_length"], 23)
        self.assertEqual(facts["status"], ReleaseStatus.COMPLETED)
        self.assertEqual(facts["trailer_youtube"], "-G9BqkgZXRA")
        self.assertAlmostEqual(facts["rating"].value, 8.1)

    def test_anilist_media_facts(self):
        media = ANILIST_SEARCH_NARUTO["data"]["Page"]["media"][0]
        identity = identity_from_media(media)
        self.assertEqual(identity.title, "Naruto")
        self.assertEqual(identity.kind, MediaKind.ANIME)
        self.assertEqual(identity.mal_id, 20)
        from functions.entertainment.sources.anilist import media_facts

        facts = media_facts(media)
        self.assertEqual(facts["studios"], ["Studio Pierrot"])
        self.assertEqual(facts["rating"].value, 8.4)
        self.assertEqual(len(facts["external_links"]), 2)

    def test_wikipedia_article_filtering(self):
        self.assertTrue(article_is_media("Digger_(2026_film)"))
        self.assertTrue(article_is_media("East_of_Eden_(2026_miniseries)"))
        self.assertTrue(article_is_media("Lanterns_(TV_series)"))
        self.assertTrue(article_is_media("Naruto_(TV_series)"))
        self.assertFalse(article_is_media("Main_Page"))
        self.assertFalse(article_is_media("Deaths_in_2026"))
        self.assertFalse(article_is_media("Verity_(novel)"))  # books aren't screen titles
        self.assertFalse(article_is_media("List_of_highest-grossing_films"))
        self.assertFalse(article_is_media("Bella_Ramsey"))  # a person, not a title
        self.assertEqual(display_title("Digger_(2026_film)"), "Digger (2026 film)")
        self.assertEqual(kind_from_article("Digger_(2026_film)"), MediaKind.MOVIE)
        self.assertEqual(kind_from_article("Lanterns_(TV_series)"), MediaKind.TV_SERIES)

    def test_wikipedia_reception_ratings(self):
        source = WikipediaSource.__new__(WikipediaSource)
        ratings = source.reception_ratings(WIKI_WIKITEXT_INTERSTELLAR)
        by_source = {r.source: r for r in ratings}
        self.assertIn("Rotten Tomatoes", by_source)
        self.assertEqual(by_source["Rotten Tomatoes"].value, 75.0)
        self.assertEqual(by_source["Rotten Tomatoes"].votes, 364)
        self.assertIn("Metacritic", by_source)
        self.assertEqual(by_source["Metacritic"].value, 74.0)

    def test_wikidata_sparql_date_precision(self):
        when, precision = parse_sparql_date("2027-01-01T00:00:00Z", "9")
        self.assertEqual(when.year, 2027)
        self.assertEqual(precision, 9)
        when, precision = parse_sparql_date("2026-12-18T00:00:00Z", "11")
        self.assertEqual((when.year, when.month, when.day), (2026, 12, 18))
        self.assertEqual(precision, 11)
        dv = DateValue(value=when, precision=9, source="Wikidata")
        self.assertEqual(dv.human(), "2026")  # year precision stays a year

    def test_websearch_streaming_domain_detection(self):
        search = FakeSearchEngine(results=[
            {"title": "Watch Interstellar | Netflix",
             "url": "https://www.netflix.com/title/70133415",
             "snippet": "Interstellar is available on Netflix.",
             "_keywords": ["where to watch", "streaming"]},
            {"title": "Interstellar - Prime Video",
             "url": "https://www.primevideo.com/detail/interstellar",
             "snippet": "Rent or buy Interstellar.",
             "_keywords": ["where to watch", "streaming"]},
            {"title": "Some blog about space movies",
             "url": "https://spaceblog.example.com/interstellar",
             "snippet": "A fan review.",
             "_keywords": ["where to watch", "streaming"]},
        ])
        source = WebSearchSource(search)

        import asyncio

        offers = asyncio.run(source.find_streaming("Interstellar"))
        platforms = {offer.platform for offer in offers}
        self.assertIn("Netflix", platforms)
        self.assertIn("Prime Video", platforms)
        self.assertNotIn("Some blog about space movies", platforms)
        for offer in offers:
            self.assertEqual(offer.source, "live web search")

    def test_websearch_rating_from_snippet(self):
        search = FakeSearchEngine(results=[
            {"title": "Interstellar (2014) - IMDb",
             "url": "https://www.imdb.com/title/tt0816692/",
             "snippet": "Directed by Christopher Nolan. Rating: 8.7/10 · 2.1M votes",
             "_keywords": ["imdb", "rating"]},
        ])
        source = WebSearchSource(search)

        import asyncio

        rating = asyncio.run(source.find_rating("Interstellar", 2014, source="imdb"))
        self.assertIsNotNone(rating)
        self.assertEqual(rating.value, 8.7)
        self.assertEqual(rating.source, "IMDb")
        self.assertEqual(rating.via, "live web search")


# ===========================================================================
# Resolver & autocomplete
# ===========================================================================


class TestResolver(unittest.TestCase):
    def _resolver(self):
        client = FakeHttpClient()
        resolver = Resolver(
            ImdbSource(client), TvMazeSource(client), KitsuSource(client), AniListSource(client)
        )
        return resolver, client

    def test_merge_and_rank_exact_beats_popular(self):
        resolver, _ = self._resolver()
        candidates = [
            Identity(title="Naruto", kind=MediaKind.ANIME, popularity=100.0),
            Identity(title="Naruto: Shippuden", kind=MediaKind.ANIME, popularity=100.0),
            Identity(title="Boruto: Naruto Next Generations", kind=MediaKind.ANIME, popularity=90.0),
        ]
        ranked = resolver.merge_and_rank("naruto", candidates)
        self.assertEqual(ranked[0].title, "Naruto")

    def test_merge_and_rank_person_loses_to_title(self):
        resolver, _ = self._resolver()
        candidates = [
            Identity(title="Naruto", kind=MediaKind.PERSON, popularity=100.0),
            Identity(title="Naruto", kind=MediaKind.TV_SERIES, popularity=50.0),
        ]
        ranked = resolver.merge_and_rank("naruto", candidates)
        self.assertEqual(ranked[0].kind, MediaKind.TV_SERIES)

    def test_merge_and_rank_merges_same_work(self):
        resolver, _ = self._resolver()
        candidates = [
            Identity(title="Naruto", kind=MediaKind.TV_SERIES, imdb_id="tt0409591", popularity=70.0),
            Identity(title="Naruto", kind=MediaKind.ANIME, kitsu_id="11", popularity=80.0,
                     alt_titles=["NARUTO"]),
        ]
        ranked = resolver.merge_and_rank("naruto", candidates)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].kind, MediaKind.ANIME)  # anime wins over generic TV
        self.assertEqual(ranked[0].imdb_id, "tt0409591")
        self.assertEqual(ranked[0].kitsu_id, "11")
        self.assertGreater(ranked[0].popularity, 80.0)  # agreement bonus

    def test_typo_tolerance(self):
        resolver, _ = self._resolver()
        candidates = [
            Identity(title="Attack on Titan", kind=MediaKind.ANIME, popularity=90.0),
            Identity(title="Interstellar", kind=MediaKind.MOVIE, popularity=90.0),
        ]
        ranked = resolver.merge_and_rank("atack on titan", candidates)
        self.assertEqual(ranked[0].title, "Attack on Titan")


class TestAutocomplete(unittest.TestCase):
    def _engine(self, payload):
        client = FakeHttpClient()
        client.route("GET", "media-imdb.com/suggestion", lambda p, u, b: payload)
        memory = TitleMemory()
        return AutocompleteEngine(ImdbSource(client), memory), memory

    def test_nar_suggests_naruto_first(self):
        engine, _ = self._engine(IMDB_SUGGEST_NARUTO)

        import asyncio

        suggestions = asyncio.run(engine.suggest("nar"))
        self.assertTrue(suggestions)
        self.assertEqual(suggestions[0].title, "Naruto")
        names = [s.title for s in suggestions]
        self.assertIn("Naruto: Shippuden", names)

    def test_one_p_suggests_one_piece_first(self):
        engine, _ = self._engine(IMDB_SUGGEST_ONEP)

        import asyncio

        suggestions = asyncio.run(engine.suggest("one p"))
        self.assertEqual(suggestions[0].title, "One Piece")
        names = [s.title for s in suggestions]
        self.assertIn("One Piece Film: Red", names)
        self.assertIn("One Punch Man", names)

    def test_choices_shape_and_values(self):
        engine, _ = self._engine(IMDB_SUGGEST_ONEP)

        import asyncio

        suggestions = asyncio.run(engine.suggest("one p"))
        choices = engine.choices(suggestions)
        self.assertTrue(5 <= len(choices) <= 10)
        for name, value in choices:
            self.assertLessEqual(len(name), 100)
            self.assertLessEqual(len(value), 100)
            self.assertNotIn("·", value)  # the insert value is a clean title
        first_value = choices[0][1]
        self.assertEqual(first_value, "One Piece")
        self.assertIn("TV Series", choices[0][0])

    def test_duplicate_titles_get_disambiguated_values(self):
        engine, _ = self._engine(IMDB_SUGGEST_ONEP)

        import asyncio

        suggestions = asyncio.run(engine.suggest("one p"))
        choices = engine.choices(suggestions)
        values = [value for _, value in choices]
        self.assertEqual(len(values), len(set(values)))  # every insert value unique

    def test_local_memory_answers_without_network(self):
        engine, memory = self._engine(_NETWORK_DOWN_PAYLOAD)

        import asyncio

        memory.remember_identities([
            Identity(title="Naruto", kind=MediaKind.ANIME, year=2002, popularity=80.0),
            Identity(title="Naruto: Shippuden", kind=MediaKind.ANIME, year=2007, popularity=85.0),
        ])
        suggestions = asyncio.run(engine.suggest("nar"))
        titles = [s.title for s in suggestions]
        self.assertIn("Naruto", titles)
        self.assertIn("Naruto: Shippuden", titles)

    def test_typo_fuzzy_score(self):
        candidate = Identity(title="Naruto", kind=MediaKind.ANIME, popularity=80.0)
        self.assertGreater(_score("naurto", candidate), 200.0)
        weak = Identity(title="Interstellar", kind=MediaKind.MOVIE, popularity=80.0)
        self.assertLess(_score("naurto", weak), _score("naurto", candidate))


_NETWORK_DOWN_PAYLOAD = {"d": []}


# ===========================================================================
# Natural-language understanding
# ===========================================================================


class TestNLU(unittest.TestCase):
    def assert_intent(self, message, kind, subject="", **fields):
        intent = match_entertainment_request(message)
        self.assertIsNotNone(intent, f"no intent parsed for {message!r}")
        self.assertEqual(intent.kind, kind, message)
        if subject is not None:
            self.assertEqual(intent.subject, subject, message)
        for key, value in fields.items():
            self.assertEqual(getattr(intent, key), value, f"{message!r} .{key}")
        return intent

    def test_recommend_examples(self):
        self.assert_intent("Recommend anime like Attack on Titan", nlu.RECOMMEND,
                           "attack on titan", media_type="anime")
        self.assert_intent("Give me movies similar to Interstellar", nlu.RECOMMEND,
                           "interstellar", media_type="movies")
        self.assert_intent("What should I watch after Breaking Bad?", nlu.RECOMMEND,
                           "breaking bad")
        self.assert_intent("Recommend something like Stranger Things but darker", nlu.RECOMMEND,
                           "stranger things", tone="darker")
        self.assert_intent("Give me underrated anime", nlu.RECOMMEND, "",
                           media_type="anime", tone="underrated")
        self.assert_intent("Best thriller movies released recently", nlu.RECOMMEND, "",
                           media_type="movies", genre="thriller")
        self.assert_intent("Give me 5 anime like Jujutsu Kaisen", nlu.RECOMMEND,
                           "jujutsu kaisen", count=5)
        self.assert_intent("Find me a short anime under 25 episodes", nlu.RECOMMEND, "",
                           media_type="anime", max_episodes=25)
        self.assert_intent("What anime should I watch after Death Note?", nlu.RECOMMEND,
                           "death note")

    def test_trending_examples(self):
        self.assert_intent("What's trending right now?", nlu.TRENDING, None)
        self.assert_intent("Trending anime", nlu.TRENDING, None, media_type="anime")
        self.assert_intent("Popular movies this week", nlu.TRENDING, None, media_type="movies")
        self.assert_intent("Top web series right now", nlu.TRENDING, None, media_type="series")
        self.assert_intent("What's everyone watching?", nlu.TRENDING, None)
        self.assert_intent("What's popular in India?", nlu.TRENDING, None, region="India")
        self.assert_intent("Most popular anime this season", nlu.TRENDING, None, media_type="anime")
        self.assert_intent("What's popular on Netflix?", nlu.TRENDING, None, platform="netflix")
        self.assert_intent("What's the highest rated movie currently trending?", nlu.TRENDING, None)

    def test_upcoming_examples(self):
        self.assert_intent("Upcoming movies", nlu.UPCOMING, "", media_type="movies")
        self.assert_intent("Upcoming anime 2027", nlu.UPCOMING, "", media_type="anime", era="2027")
        self.assert_intent("Upcoming Netflix shows", nlu.UPCOMING, "",
                           media_type="series", platform="netflix")
        self.assert_intent("Upcoming Marvel movies", nlu.UPCOMING, "marvel", media_type="movies")
        self.assert_intent("Anime releasing next season", nlu.UPCOMING, "",
                           media_type="anime", era="next season")
        self.assert_intent("What movies are coming this month?", nlu.UPCOMING, "",
                           media_type="movies", era="this month")
        self.assert_intent("What comes out this Friday?", nlu.UPCOMING, None)
        self.assert_intent("Best releases this month", nlu.UPCOMING, "")
        self.assert_intent("Upcoming highly rated movies", nlu.UPCOMING, "", media_type="movies")
        self.assert_intent("Is Chainsaw Man season 2 coming?", nlu.UPCOMING,
                           "chainsaw man", season=2)

    def test_streaming_examples(self):
        self.assert_intent("Where can I watch Oppenheimer in India?", nlu.STREAMING,
                           "oppenheimer", region="India")
        self.assert_intent("Is Interstellar on Netflix?", nlu.STREAMING,
                           "interstellar", platform="netflix")
        intent = match_entertainment_request("where can I watch demon Slayer?")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.kind, nlu.STREAMING)
        self.assertEqual(intent.subject, "demon slayer")

    def test_rating_and_info_examples(self):
        self.assert_intent("What's the rating of Interstellar?", nlu.RATING, "interstellar")
        self.assert_intent("Dune rating", nlu.RATING, "dune")
        self.assert_intent("Tell me everything about Dune.", nlu.INFO, "dune")

    def test_when_release_examples(self):
        self.assert_intent("When is Dune releasing?", nlu.UPCOMING, "dune")
        self.assert_intent("When does Stranger Things season 5 come out?", nlu.UPCOMING,
                           "stranger things", season=5)

    def test_no_false_positives(self):
        for message in (
            "how do I cook pasta",
            "what time is it",
            "I like movies",
            "that song is popular",
            "check https://example.com/naruto",
            "top 10 cooking tips for dinner",
            "that new album drops friday",
            "i had a dream about a movie last night",
            "my movie is rendering",
        ):
            self.assertIsNone(match_entertainment_request(message), message)

    def test_parse_recommend_query(self):
        intent = parse_recommend_query("like Interstellar")
        self.assertEqual(intent.subject, "interstellar")
        intent = parse_recommend_query("underrated horror movies")
        self.assertEqual(intent.subject, "")
        self.assertEqual(intent.genre, "horror")
        intent = parse_recommend_query("Attack on Titan")
        self.assertEqual(intent.subject, "Attack on Titan")
        intent = parse_recommend_query("short anime under 25 episodes")
        self.assertEqual(intent.subject, "")
        self.assertEqual(intent.max_episodes, 25)


# ===========================================================================
# Caches & verification
# ===========================================================================


class TestCacheAndVerify(unittest.TestCase):
    def test_ttl_cache_expiry(self):
        cache = TTLCache(max_items=4, ttl=3600.0)
        cache.put("a", 1, ttl=0.0)
        self.assertIsNone(cache.get("a"))

    def test_media_cache_volatile_expiry(self):
        cache = MediaCache(stable_ttl=3600, volatile_ttl=0.0)
        title = MediaTitle(title="X")
        cache.put_stable("x", title)
        self.assertIs(cache.get_stable("x"), title)
        self.assertFalse(cache.volatile_fresh("x"))

    def test_title_memory_prefix_search(self):
        memory = TitleMemory()
        memory.remember_identities([
            Identity(title="Naruto", kind=MediaKind.ANIME, year=2002),
            Identity(title="One Piece", kind=MediaKind.ANIME, year=1999),
            Identity(title="Naruto: Shippuden", kind=MediaKind.ANIME, year=2007),
        ])
        hits = memory.search("naruto")
        titles = {i.title for i in hits}
        self.assertEqual(titles, {"Naruto", "Naruto: Shippuden"})

    def test_check_years_conflict(self):
        from functions.entertainment.verify import Observation

        conflict = check_years([
            Observation(source="IMDb", value="2014"),
            Observation(source="Wikipedia", value="2014"),
        ])
        self.assertIsNone(conflict)
        conflict = check_years([
            Observation(source="IMDb", value="2013"),
            Observation(source="Wikipedia", value="2016"),
        ])
        self.assertIsNotNone(conflict)
        self.assertIn("differs", conflict.note)

    def test_verify_title_notes_conflicts(self):
        identity = Identity(title="X", year=2013, source="IMDb")
        title = MediaTitle(
            title="X",
            identity=identity,
            release=DateValue.from_parts(2016, 5, 1, source="Wikipedia"),
        )
        notes = verify_title(title)
        self.assertTrue(any("differs" in note for note in notes))

    def test_dedupe_ratings(self):
        ratings = [
            Rating(source="IMDb", value=8.7),
            Rating(source="IMDb", value=8.4, via="live web search"),
            Rating(source="Rotten Tomatoes", value=75.0, scale="%"),
        ]
        deduped = dedupe_ratings(ratings)
        self.assertEqual(len(deduped), 2)
        # The direct database rating wins over the scraped one.
        imdb = [r for r in deduped if r.source == "IMDb"][0]
        self.assertEqual(imdb.value, 8.7)


# ===========================================================================
# End-to-end service lookups (fixtures wired like the live pipeline)
# ===========================================================================


class TestServiceLookup(unittest.IsolatedAsyncioTestCase):
    async def test_lookup_naruto_full_anime_card(self):
        client = FakeHttpClient()
        search = FakeSearchEngine(results=[])
        wire_naruto_world(client, search)
        add_wiki_summaries(client, {
            "Naruto_(TV_series)": WIKI_SUMMARY_NARUTO_TV,
        })
        # wikitext route for the Naruto article
        def wiki_action_naruto(params, url, body):
            if params.get("action") == "opensearch":
                return ["naruto", ["Naruto (TV series)"], [""],
                        ["https://en.wikipedia.org/wiki/Naruto_(TV_series)"]]
            return {
                "batchcomplete": True,
                "query": {"pages": [{
                    "pageid": 1, "ns": 0, "title": "Naruto (TV series)",
                    "revisions": [{"slots": {"main": {"content": WIKI_WIKITEXT_NARUTO_TV}}}],
                }]},
            }
        client.routes = [r for r in client.routes if "wikipedia.org/w/api.php" not in r[1]]
        client.route("GET", "wikipedia.org/w/api.php", wiki_action_naruto)

        service = make_service(client, search)
        outcome = await service.lookup("Naruto")

        self.assertTrue(outcome.found, outcome.status)
        title = outcome.title
        self.assertEqual(title.title, "Naruto")
        self.assertTrue(title.kind.is_anime)
        # Episodes from the anime databases (220), not TVmaze's season math.
        self.assertEqual(title.episodes, 220)
        self.assertEqual(title.seasons, 5)  # TVMaze seasons
        # Multiple named rating sources, never blended.
        sources = {rating.source for rating in title.ratings}
        self.assertIn("IMDb", sources)
        self.assertIn("MyAnimeList", sources)
        self.assertIn("Kitsu", sources)
        self.assertIn("AniList", sources)
        self.assertIn("TVmaze", sources)
        for rating in title.ratings:
            self.assertTrue(rating.scale)
        # Streaming offers come from structured links.
        platforms = {offer.platform for offer in title.streaming}
        self.assertIn("Crunchyroll", platforms)
        # Franchise relations surfaced.
        related_titles = [r.title for r in title.similar]
        self.assertIn("Naruto: Shippuden", related_titles)
        sequels = [r for r in title.similar if r.relation == "sequel"]
        self.assertTrue(sequels)
        # Studio + creator info.
        self.assertIn("Studio Pierrot", title.studios)
        self.assertTrue(title.synopsis)
        self.assertEqual(title.trailer_url, "https://www.youtube.com/watch?v=-G9BqkgZXRA")
        # The card renders with the expected shape.
        card = format_title_card(title)
        self.assertIn("⭐ **Rating:**", card)
        self.assertIn("AniList", card)
        self.assertIn("📅 **Release:**", card)
        self.assertIn("**📖 Synopsis**", card)
        self.assertIn("📡 **Where to watch:**", card)

    async def test_lookup_interstellar_movie_card(self):
        client = FakeHttpClient()
        search = FakeSearchEngine(results=[
            {"title": "Watch Interstellar | Netflix",
             "url": "https://www.netflix.com/title/70133415",
             "snippet": "Interstellar is available on Netflix.",
             "_keywords": ["where to watch", "streaming"]},
            {"title": "12 Movies Like Interstellar",
             "url": "https://reelgood.com/list/like-interstellar",
             "snippet": "Inception, Arrival, and The Martian top this list of mind-bending sci-fi.",
             "_keywords": ["movies and shows like"]},
            {"title": "Interstellar (2014) - IMDb",
             "url": "https://www.imdb.com/title/tt0816692/",
             "snippet": "Rating: 8.7/10 · 2.1M votes",
             "_keywords": ["imdb", "rating"]},
        ])
        wire_interstellar_world(client, search)
        add_wiki_summaries(client, {"Interstellar_(film)": WIKI_SUMMARY_INTERSTELLAR})

        service = make_service(client, search)
        outcome = await service.lookup("Interstellar")

        self.assertTrue(outcome.found, outcome.status)
        title = outcome.title
        self.assertEqual(title.kind, MediaKind.MOVIE)
        self.assertIn("Christopher Nolan", title.creators)
        self.assertEqual(title.runtime_minutes, 169)
        self.assertEqual(title.runtime_text(), "2h 49m")
        cast_names = title.cast_names(10)
        self.assertIn("Matthew McConaughey", cast_names)
        self.assertIn("Anne Hathaway", cast_names)
        rating_sources = {r.source for r in title.ratings}
        self.assertIn("IMDb", rating_sources)
        self.assertIn("Rotten Tomatoes", rating_sources)
        self.assertIn("Metacritic", rating_sources)
        # Release date comes from the Wikipedia infobox premiere.
        self.assertIsNotNone(title.release)
        self.assertEqual(title.release.value.year, 2014)
        # Streaming via live web search.
        platforms = {offer.platform for offer in title.streaming}
        self.assertIn("Netflix", platforms)
        # Web-mined similar titles, verified against IMDb.
        similar_titles = [p.title for p in title.similar]
        self.assertTrue(set(similar_titles) & {"Inception", "Arrival", "The Martian"})
        card = format_title_card(title)
        self.assertIn("Interstellar", title_heading(title))
        self.assertIn("⏱️ **Runtime:** 2h 49m", card)

    async def test_lookup_ambiguous_short_query(self):
        client = FakeHttpClient()
        client.routes = []
        client.route("GET", "media-imdb.com/suggestion", lambda p, u, b: IMDB_SUGGEST_IT)
        client.route("GET", "kitsu.app/api/edge/anime", lambda p, u, b: {"data": []})
        client.route("POST", "graphql.anilist.co", lambda p, u, b: {"data": {"Page": {"media": []}}})
        client.route("GET", "tvmaze.com/search/shows", lambda p, u, b: [])
        service = make_service(client, FakeSearchEngine(results=[]))
        outcome = await service.lookup("it")
        self.assertEqual(outcome.status, AMBIGUOUS)
        self.assertTrue(outcome.candidates)
        self.assertEqual(outcome.candidates[0].title, "It")

    async def test_lookup_not_found(self):
        client = FakeHttpClient()
        client.route("GET", "media-imdb.com/suggestion", lambda p, u, b: {"d": []})
        client.route("GET", "kitsu.app/api/edge/anime", lambda p, u, b: {"data": []})
        client.route("POST", "graphql.anilist.co", lambda p, u, b: {"data": {"Page": {"media": []}}})
        client.route("GET", "tvmaze.com/search/shows", lambda p, u, b: [])
        service = make_service(client, FakeSearchEngine(results=[]))
        outcome = await service.lookup("zzzzqqqxyzzy")
        self.assertEqual(outcome.status, NOT_FOUND)

    async def test_lookup_network_down_is_honest(self):
        client = FakeHttpClient()  # no routes at all → everything fails
        service = make_service(client, FakeSearchEngine(results=[]))
        with self.assertRaises(ParadiseError) as raised:
            await service.lookup("naruto")
        self.assertIn("couldn't verify", str(raised.exception))

    async def test_lookup_uses_cache_within_volatile_window(self):
        client = FakeHttpClient()
        search = FakeSearchEngine(results=[])
        wire_naruto_world(client, search)
        add_wiki_summaries(client, {"Naruto_(TV_series)": WIKI_SUMMARY_NARUTO_TV})
        service = make_service(client, search)
        outcome = await service.lookup("Naruto")
        self.assertTrue(outcome.found)

        # Now kill the network: within the volatile window the cached
        # card is still served without touching it.
        client.routes = []
        service.imdb.last_error = ""
        second = await service.lookup("Naruto")
        self.assertTrue(second.found)
        self.assertFalse(second.stale)

    async def test_lookup_serves_stale_card_when_refresh_fails(self):
        client = FakeHttpClient()
        search = FakeSearchEngine(results=[])
        wire_naruto_world(client, search)
        add_wiki_summaries(client, {"Naruto_(TV_series)": WIKI_SUMMARY_NARUTO_TV})
        service = make_service(client, search)
        first = await service.lookup("Naruto")
        self.assertTrue(first.found)

        # Force the volatile window to expire, then break the network.
        service.media_cache._volatile_keys.pop("naruto")
        client.routes = []
        second = await service.lookup("Naruto")
        self.assertTrue(second.found)
        self.assertTrue(second.stale)


class TestServiceReports(unittest.IsolatedAsyncioTestCase):
    async def test_trending_report(self):
        client = FakeHttpClient()
        search = FakeSearchEngine(results=[])
        wire_naruto_world(client, search)

        def wiki_pageviews(params, url, body):
            return WIKI_PAGEVIEWS_TOP
        client.routes = [r for r in client.routes if "wikimedia.org" not in r[1]]
        client.route("GET", "wikimedia.org", wiki_pageviews)

        service = make_service(client, search)
        heading, entries, notes = await service.trending_report(mode="trending")
        self.assertTrue(entries)
        titles = [e.title for e in entries]
        self.assertIn("Digger (2026 film)", titles)
        self.assertIn("Death Note", titles)  # AniList trending fixture
        # Novels and people are filtered out.
        self.assertNotIn("Verity (novel)", titles)
        self.assertNotIn("Bella Ramsey", titles)
        # Each entry carries its metric + honest list labels.
        self.assertTrue(all(e.metric for e in entries))
        self.assertTrue(any(e.list_kind == "trending" for e in entries))
        self.assertTrue(any("Wikipedia" in note for note in notes))
        text = format_trending(entries, notes, heading)
        self.assertIn("🔥 **", text)

        # Category filter: movies only.
        _, movies, _ = await service.trending_report(category="movies", mode="trending")
        self.assertTrue(movies)
        self.assertTrue(all(e.kind is MediaKind.MOVIE for e in movies))

    async def test_upcoming_report_precision(self):
        client = FakeHttpClient()
        search = FakeSearchEngine(results=[])
        wire_naruto_world(client, search)
        service = make_service(client, search)
        heading, entries, notes = await service.upcoming_report(category="movies", days=365)
        self.assertTrue(entries)
        by_title = {e.title: e for e in entries}
        self.assertIn("Dune: Part Three", by_title)
        self.assertEqual(by_title["Dune: Part Three"].when_text(), "18 December 2026")
        # Year precision stays a year — never a fake exact date.
        self.assertEqual(by_title["Supergirl"].when_text(), "2027")
        self.assertTrue(any("not an exact day" in e.status_note for e in entries))
        # Raw Q-id labels (label service miss) are skipped.
        self.assertNotIn("Q1003", by_title)
        text = format_upcoming(entries, notes, heading)
        self.assertIn("🗓️ **", text)

    async def test_upcoming_series_from_schedule(self):
        client = FakeHttpClient()
        search = FakeSearchEngine(results=[])
        wire_naruto_world(client, search)
        service = make_service(client, search)
        heading, entries, notes = await service.upcoming_report(
            category="series", days=2
        )
        titles = [e.title for e in entries]
        self.assertIn("Wednesday", titles)
        # low-weight shows don't make the cut
        self.assertNotIn("Some Obscure Anime", titles)
        wednesday = next(e for e in entries if e.title == "Wednesday")
        self.assertEqual(wednesday.platform, "Netflix")
        self.assertEqual(wednesday.status_note, "series premiere")

    async def test_recommend_report_with_subject(self):
        client = FakeHttpClient()
        search = FakeSearchEngine(results=[])
        wire_naruto_world(client, search)
        add_wiki_summaries(client, {"Naruto_(TV_series)": WIKI_SUMMARY_NARUTO_TV})
        service = make_service(client, search)
        heading, picks, intro = await service.recommend_report(subject="Naruto")
        self.assertTrue(picks)
        self.assertTrue(all(p.why for p in picks))
        text = format_recommendations(picks, heading, intro)
        self.assertIn("why:", text)
        # Every pick explains itself.
        for pick in picks:
            self.assertTrue(pick.why or pick.rating)

    async def test_streaming_report_region_aware(self):
        client = FakeHttpClient()
        search = FakeSearchEngine(results=[
            {"title": "Watch Naruto | Netflix",
             "url": "https://www.netflix.com/title/naruto",
             "snippet": "Naruto is available on Netflix India.",
             "_keywords": ["india", "streaming"]},
        ])
        wire_naruto_world(client, search)
        service = make_service(client, search)
        heading, body = await service.streaming_report(subject="Naruto", region="India")
        self.assertIn("Naruto", heading)
        self.assertIn("India", heading)
        self.assertIn("Crunchyroll", body)  # structured link
        self.assertIn("Netflix", body)      # live web verification
        self.assertIn("region checked: India", body)

    async def test_rating_report(self):
        client = FakeHttpClient()
        search = FakeSearchEngine(results=[])
        wire_naruto_world(client, search)
        add_wiki_summaries(client, {"Naruto_(TV_series)": WIKI_SUMMARY_NARUTO_TV})
        service = make_service(client, search)
        heading, body = await service.rating_report("Naruto")
        self.assertIn("ratings", heading)
        self.assertIn("IMDb", body)
        self.assertIn("MyAnimeList", body)
        # Ratings are never blended into one number.
        self.assertIn("•", body)
        self.assertIn("different rating systems", body)


# ===========================================================================
# Formatter
# ===========================================================================


class TestFormatter(unittest.TestCase):
    def _title(self) -> MediaTitle:
        from functions.entertainment.models import Person

        identity = Identity(
            title="Interstellar", kind=MediaKind.MOVIE, year=2014,
            imdb_id="tt0816692", source="IMDb",
        )
        title = MediaTitle(
            title="Interstellar", kind=MediaKind.MOVIE, identity=identity,
        )
        title.ratings = [
            Rating(source="IMDb", value=8.7, votes=2_100_000),
            Rating(source="Rotten Tomatoes", value=75.0, scale="%", votes=364,
                   via="Wikipedia (citing Rotten Tomatoes)"),
        ]
        title.release = DateValue.from_parts(2014, 11, 7, source="Wikipedia")
        title.genres = ["Sci-Fi", "Drama", "Adventure"]
        title.runtime_minutes = 169
        title.creators = ["Christopher Nolan"]
        title.cast = [Person(name=f"Actor {i}") for i in range(6)]
        title.synopsis = "A team of explorers travel through a wormhole in space."
        title.streaming = [StreamingOffer(platform="Netflix", source="live web search")]
        title.similar = [
            RelatedTitle(title="Inception", year=2010, why="frequently named alongside it"),
        ]
        title.status = ReleaseStatus.RELEASED
        return title

    def test_card_follows_spec_structure(self):
        card = format_title_card(self._title())
        self.assertIn("⭐ **Rating:** IMDb: 8.7/10 (2.1M votes)", card)
        self.assertIn("Rotten Tomatoes: 75%", card)
        self.assertIn("📅 **Release:**", card)
        self.assertIn("🎭 **Genre:**", card)
        self.assertIn("⏱️ **Runtime:** 2h 49m", card)
        self.assertIn("🎬 **Director:** Christopher Nolan", card)
        self.assertIn("🌐 **Status:** Released", card)
        self.assertIn("**📖 Synopsis**", card)
        self.assertIn("📡 **Where to watch:**", card)
        self.assertIn("Netflix", card)
        self.assertIn("**🍿 You might also like**", card)
        self.assertIn("Inception", card)
        self.assertLessEqual(len(card), 3900)

    def test_series_card_shows_episodes(self):
        from functions.entertainment.models import Person

        identity = Identity(title="Naruto", kind=MediaKind.ANIME, year=2002)
        title = MediaTitle(title="Naruto", kind=MediaKind.ANIME, identity=identity)
        title.seasons = 5
        title.episodes = 220
        title.episode_runtime = 23
        title.status = ReleaseStatus.COMPLETED
        card = format_title_card(title)
        self.assertIn("🎞️ **Episodes:** 5 seasons, 220 episodes, ~23m per episode", card)
        self.assertIn("Status", card)

    def test_upcoming_card_never_fakes_dates(self):
        entries = [
            UpcomingEntry(
                title="Supergirl",
                when=DateValue(value=date(2027, 1, 1), precision=DateValue.YEAR, source="Wikidata"),
                status_note="window announced",
            )
        ]
        text = format_upcoming(entries, [], "Coming up")
        self.assertIn("**Supergirl** — 2027", text)
        self.assertNotIn("1 January 2027", text)


# ===========================================================================
# The cog's chat pipeline (needs no Discord connection)
# ===========================================================================


class FakeBot:
    config = None
    user = None

    def get_cog(self, _name):
        return None


class FakeMessage:
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class TestChatPipeline(unittest.IsolatedAsyncioTestCase):
    async def _cog_with_naruto_world(self):
        from commands.entertainment import Entertainment

        client = FakeHttpClient()
        search = FakeSearchEngine(results=[
            {"title": "Watch Naruto | Netflix",
             "url": "https://www.netflix.com/title/naruto",
             "snippet": "Available on Netflix.",
             "_keywords": ["where to watch", "streaming"]},
        ])
        wire_naruto_world(client, search)
        add_wiki_summaries(client, {"Naruto_(TV_series)": WIKI_SUMMARY_NARUTO_TV})

        def wiki_action_naruto(params, url, body):
            if params.get("action") == "opensearch":
                return ["naruto", ["Naruto (TV series)"], [""],
                        ["https://en.wikipedia.org/wiki/Naruto_(TV_series)"]]
            return {
                "query": {"pages": [{
                    "title": "Naruto (TV series)",
                    "revisions": [{"slots": {"main": {"content": WIKI_WIKITEXT_NARUTO_TV}}}],
                }]},
            }
        client.routes = [r for r in client.routes if "wikipedia.org/w/api.php" not in r[1]]
        client.route("GET", "wikipedia.org/w/api.php", wiki_action_naruto)

        cog = Entertainment.__new__(Entertainment)  # skip __init__'s real service
        cog.bot = FakeBot()
        cog.settings = None
        cog.service = make_service(client, search)
        cog._user_last = {}
        cog._channel_last = {}
        return cog

    async def test_recommend_chat(self):
        cog = await self._cog_with_naruto_world()
        intent = cog.service.parse_chat("recommend anime like Naruto")
        self.assertIsNotNone(intent)
        embed = await cog._answer_intent(intent, FakeMessage())
        self.assertTrue(embed.title)
        self.assertIn("Naruto", embed.title)
        self.assertIn("🍿", embed.description or embed.title)

    async def test_trending_chat(self):
        cog = await self._cog_with_naruto_world()
        intent = cog.service.parse_chat("what's trending right now?")
        self.assertIsNotNone(intent)
        embed = await cog._answer_intent(intent, FakeMessage())
        self.assertIn("Trending", embed.title)

    async def test_streaming_chat_with_region(self):
        cog = await self._cog_with_naruto_world()
        intent = cog.service.parse_chat("where can I watch Naruto in India?")
        self.assertIsNotNone(intent)
        embed = await cog._answer_intent(intent, FakeMessage())
        self.assertIn("Where to watch Naruto", embed.title)
        self.assertIn("India", embed.title)

    async def test_rating_chat(self):
        cog = await self._cog_with_naruto_world()
        intent = cog.service.parse_chat("what's the rating of Naruto?")
        self.assertIsNotNone(intent)
        embed = await cog._answer_intent(intent, FakeMessage())
        self.assertIn("ratings", embed.title)
        self.assertIn("IMDb", embed.description)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
