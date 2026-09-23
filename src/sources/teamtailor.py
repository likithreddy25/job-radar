"""Teamtailor career site adapter."""
from __future__ import annotations

from .generic_html import GenericHTMLBoardSource, board_id, slug_from_url


def _board_id(board_url: str) -> str:
    return board_id("teamtailor", board_url)


def _slug(board_url: str) -> str:
    return slug_from_url(board_url, "teamtailor")


class TeamtailorSource(GenericHTMLBoardSource):
    platform = "teamtailor"
    link_patterns = ("/jobs/",)
