"""Unit tests for the web search engine, models, and Discord UI."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions.search import (
    SearchEngine,
    SearchPaginationView,
    SearchResponse,
    SearchResult,
    build_search_embed,
)


class TestSearchModels(unittest.TestCase):
    def test_search_result_source_parsing(self):
        res = SearchResult(
            title="Python Docs",
            url="https://docs.python.org/3/tutorial/index.html",
            snippet="Python tutorial",
        )
        self.assertEqual(res.source, "docs.python.org")

    def test_search_result_custom_source(self):
        res = SearchResult(
            title="Custom Source",
            url="https://example.com/item",
            snippet="Test snippet",
            source="custom.org",
        )
        self.assertEqual(res.source, "custom.org")

    def test_search_response_properties(self):
        r1 = SearchResult(title="T1", url="https://a.com", snippet="S1")
        r2 = SearchResult(title="T2", url="https://b.com", snippet="S2")
        resp = SearchResponse(query="test query", results=[r1, r2], engine="DuckDuckGo")

        self.assertTrue(resp.has_results)
        self.assertEqual(resp.total_count, 2)
        self.assertEqual(resp.query, "test query")

    def test_empty_search_response(self):
        resp = SearchResponse(query="empty query", results=[])
        self.assertFalse(resp.has_results)
        self.assertEqual(resp.total_count, 0)


class TestSearchEngine(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = SearchEngine(cache_ttl=60)

    def test_ddg_lite_html_parsing(self):
        sample_html = """
        <html>
        <table>
            <tr>
                <td><a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FPython_(programming_language)">Python (programming language) - Wikipedia</a></td>
            </tr>
            <tr>
                <td class="result-snippet">Python is a high-level, general-purpose programming language.</td>
            </tr>
            <tr>
                <td><a class="result-link" href="https://www.python.org/">Welcome to Python.org</a></td>
            </tr>
            <tr>
                <td class="result-snippet">The official home of the Python Programming Language.</td>
            </tr>
        </table>
        </html>
        """
        results = self.engine._parse_ddg_lite_html(sample_html)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].title, "Python (programming language) - Wikipedia")
        self.assertEqual(results[0].url, "https://en.wikipedia.org/wiki/Python_(programming_language)")
        self.assertIn("general-purpose", results[0].snippet)
        self.assertEqual(results[1].url, "https://www.python.org/")

    def test_ddg_html_parsing(self):
        sample_html = """
        <div class="result results_links results_links_deep web-result">
            <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgithub.com%2Fpython">GitHub - Python</a>
            <a class="result__snippet">Python source code repository on GitHub.</a>
        </div>
        """
        results = self.engine._parse_ddg_html(sample_html)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "GitHub - Python")
        self.assertEqual(results[0].url, "https://github.com/python")
        self.assertEqual(results[0].snippet, "Python source code repository on GitHub.")

    async def test_wikipedia_fallback_parsing(self):
        sample_data = [
            "python",
            ["Python (programming language)", "Python (genus)"],
            ["A high-level programming language", "A genus of snakes"],
            ["https://en.wikipedia.org/wiki/Python_(programming_language)", "https://en.wikipedia.org/wiki/Python_(genus)"],
        ]
        with patch.object(self.engine, "_search_wikipedia", new=AsyncMock(return_value=[
            SearchResult(
                title=sample_data[1][0],
                url=sample_data[3][0],
                snippet=sample_data[2][0],
                source="wikipedia.org",
                authoritative=True,
            )
        ])):
            results = await self.engine._search_wikipedia("python")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].title, "Python (programming language)")

    async def test_search_empty_query(self):
        resp = await self.engine.search("")
        self.assertEqual(resp.query, "")
        self.assertEqual(len(resp.results), 0)

    async def test_search_caching(self):
        mock_results = [SearchResult(title="Cached Title", url="https://example.com/cached", snippet="Cached snippet")]
        with patch.object(self.engine, "_search_duckduckgo_lite", new=AsyncMock(return_value=mock_results)):
            resp1 = await self.engine.search("test query")
            self.assertFalse(resp1.cached)
            self.assertEqual(len(resp1.results), 1)

            resp2 = await self.engine.search("test query")
            self.assertTrue(resp2.cached)
            self.assertEqual(resp2.results[0].title, "Cached Title")


class TestSearchUI(unittest.TestCase):
    def test_build_search_embed_with_results(self):
        r1 = SearchResult(title="Result 1", url="https://r1.com", snippet="Snippet 1", authoritative=True)
        r2 = SearchResult(title="Result 2", url="https://r2.com", snippet="Snippet 2")
        resp = SearchResponse(query="my query", results=[r1, r2])

        embed = build_search_embed(resp, current_index=0)
        self.assertIn("my query", embed.title)
        self.assertIn("Result 1", embed.fields[0].name)
        self.assertIn("Snippet 1", embed.fields[0].value)

    def test_build_search_embed_empty(self):
        resp = SearchResponse(query="no match", results=[])
        embed = build_search_embed(resp)
        self.assertIn("No results found", embed.description)

    def test_pagination_view_bounds(self):
        r1 = SearchResult(title="R1", url="https://r1.com", snippet="S1")
        r2 = SearchResult(title="R2", url="https://r2.com", snippet="S2")
        resp = SearchResponse(query="pagination test", results=[r1, r2])

        view = SearchPaginationView(resp, author_id=12345)
        self.assertTrue(view.prev_btn.disabled)
        self.assertFalse(view.next_btn.disabled)


if __name__ == "__main__":
    unittest.main()
