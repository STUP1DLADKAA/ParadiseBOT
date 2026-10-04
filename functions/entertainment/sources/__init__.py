"""The key-less data providers.

Each module exposes small async functions that take the shared
:class:`~functions.entertainment.http.HttpClient` and return plain data
(or raise :class:`HttpError` — callers treat every source as optional).
"""

from __future__ import annotations
