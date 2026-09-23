"""Wellfound company jobs page adapter."""
from __future__ import annotations

from .generic_html import GenericHTMLBoardSource, board_id, slug_from_url


def _board_id(board_url: str) -> str:
    return board_id("wellfound", board_url)


def _slug(board_url: str) -> str:
    return slug_from_url(board_url, "wellfound")


class WellfoundSource(GenericHTMLBoardSource):
    platform = "wellfound"
    link_patterns = ("/jobs/", "/job/", "/l/")
