"""Y Combinator Work at a Startup adapter."""
from __future__ import annotations

from .generic_html import GenericHTMLBoardSource, board_id, slug_from_url


def _board_id(board_url: str) -> str:
    return board_id("workatastartup", board_url)


def _slug(board_url: str) -> str:
    return slug_from_url(board_url, "workatastartup")


class WorkAtAStartupSource(GenericHTMLBoardSource):
    platform = "workatastartup"
    link_patterns = ("/jobs/", "/companies/", "/job_posts/")
