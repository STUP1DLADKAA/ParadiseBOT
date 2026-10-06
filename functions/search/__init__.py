"""Search package for Paradise."""

from .answers import AnswerEngine
from .engine import SearchEngine
from .models import (
    EMPTY,
    OK,
    PARSE_ERROR,
    PROVIDER_ERROR,
    SearchAnswer,
    SearchResponse,
    SearchResult,
)
from .ui import (
    SearchAnswerView,
    SearchPaginationView,
    build_answer_embed,
    build_search_embed,
)

__all__ = [
    "EMPTY",
    "OK",
    "PARSE_ERROR",
    "PROVIDER_ERROR",
    "AnswerEngine",
    "SearchAnswer",
    "SearchAnswerView",
    "SearchEngine",
    "SearchResult",
    "SearchResponse",
    "SearchPaginationView",
    "build_answer_embed",
    "build_search_embed",
]
