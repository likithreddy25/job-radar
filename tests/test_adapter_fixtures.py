from __future__ import annotations

from pathlib import Path
import unittest

from src.sources.base import find_jobposting_ldjson, jobposting_location_text
from src.sources.generic_html import extract_links_by_patterns, parse_recruitee_jobs
from src.sources.icims import _find_detail_links, _find_next_page
from src.sources.jobvite import _find_job_links
from src.sources.netflix import _clean_location_name, _parse_netflix_payload


FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class AdapterFixtureTests(unittest.TestCase):
    def test_generic_html_link_extraction_keeps_same_host_matches(self) -> None:
        html = """
        <html><body>
          <a href="/jobs/software-engineer">Software Engineer</a>
          <a href="https://careers.example.com/jobs/data-engineer/">Data Engineer</a>
          <a href="https://external.example.org/jobs/ignore-me">Ignore</a>
          <a href="/about">About</a>
        </body></html>
        """
        links = extract_links_by_patterns(
            html,
            "https://careers.example.com/openings",
            ("/jobs/",),
        )
        self.assertEqual(
            links,
            [
                "https://careers.example.com/jobs/software-engineer",
                "https://careers.example.com/jobs/data-engineer",
            ],
        )

    def test_recruitee_payload_parse(self) -> None:
        payload = {
            "offers": [
                {
                    "id": "job-123",
                    "title": "Software Engineer",
                    "careers_url": "https://company.recruitee.com/o/software-engineer",
                    "locations": [{"name": "New York, NY"}],
                    "published_at": "2026-05-08T10:00:00Z",
                    "description": "<p>Build product systems.</p>",
                }
            ]
        }
        jobs = parse_recruitee_jobs(
            payload,
            company="Example Co",
            platform_slug="company-recruitee-com",
            board_url="https://company.recruitee.com",
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].key, "recruitee:company-recruitee-com:job-123")
        self.assertEqual(jobs[0].company, "Example Co")
        self.assertEqual(jobs[0].title, "Software Engineer")
        self.assertEqual(jobs[0].location, "New York, NY")
        self.assertEqual(jobs[0].url, "https://company.recruitee.com/o/software-engineer")
        self.assertEqual(jobs[0].posted, "2026-05-08T10:00:00Z")
        self.assertIn("Build product systems", jobs[0].description)

    def test_jobvite_listing_links(self) -> None:
        html = _read("jobvite_listing.html")
        links = _find_job_links(html, "https://jobs.jobvite.com/absolute/job")
        self.assertEqual(len(links), 2)
        self.assertTrue(links[0].startswith("https://jobs.jobvite.com/absolute/job/"))

    def test_jobvite_ldjson_parse(self) -> None:
        html = _read("jobvite_detail.html")
        schema = find_jobposting_ldjson(html)
        self.assertEqual(schema["title"], "Account Executive, India")
        self.assertEqual(jobposting_location_text(schema), "Bengaluru, KA, IN")

    def test_icims_search_links_and_next_page(self) -> None:
        html = _read("icims_search_iframe.html")
        links = _find_detail_links(html, "https://careers-kloveair1.icims.com/jobs/search?ss=1&in_iframe=1")
        next_page = _find_next_page(html, "https://careers-kloveair1.icims.com/jobs/search?ss=1&in_iframe=1")
        self.assertEqual(len(links), 1)
        self.assertIn("/jobs/2387/network-engineer/job", links[0])
        self.assertIn("pr=1", next_page)

    def test_icims_ldjson_parse(self) -> None:
        html = _read("icims_detail.html")
        schema = find_jobposting_ldjson(html)
        self.assertEqual(schema["title"], "Network Engineer")
        self.assertEqual(jobposting_location_text(schema), "Franklin, TN, US")

    def test_netflix_payload_and_location_cleanup(self) -> None:
        html = _read("netflix_detail.html")
        payload = _parse_netflix_payload(html)
        self.assertEqual(payload["positions"][0]["id"], "790315374877")
        self.assertEqual(_clean_location_name("Panamá, Provincia de Panamá,PA, PA"), "Panamá, Provincia de Panamá, PA")


if __name__ == "__main__":
    unittest.main()
