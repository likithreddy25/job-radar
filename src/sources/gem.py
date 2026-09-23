"""Gem-hosted career page adapter."""
from __future__ import annotations

from .generic_html import GenericHTMLBoardSource, board_id, slug_from_url


def _board_id(board_url: str) -> str:
    return board_id("gem", board_url)


def _slug(board_url: str) -> str:
    return slug_from_url(board_url, "gem")


class GemSource(GenericHTMLBoardSource):
    platform = "gem"
    link_patterns = ("/jobs/", "/job/", "/positions/")
