import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.main import _dispatch_results
from src.sources.base import Job


class _FakeEvaluation:
    def __init__(self, *, score: int, label: str) -> None:
        self.score = score
        self.label = label


class _FakeDatabase:
    def __init__(self) -> None:
        self.new_keys: set[str] = set()

    def get_feature_flags(self, defaults: dict) -> dict:
        return defaults

    def inspect_job(self, **_: object) -> dict:
        return {
            "structured": {},
            "is_repost": False,
            "repost_of_key": "",
            "canonical_key": "",
            "employer_quality_score": 50,
            "employer_quality_reason": "",
        }

    def is_new_job(self, key: str) -> bool:
        return key in self.new_keys

    def mark_job_seen(self, **_: object) -> None:
        raise AssertionError("mark_job_seen should not be called in dry-run tests")

    def expire_old_jobs(self, days: int) -> None:
        raise AssertionError(f"expire_old_jobs should not be called in dry-run tests: {days}")


class _FakeNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[list[Job], list[Job], str, str, list[str] | None]] = []

    def notify(
        self,
        yes_jobs: list[Job],
        maybe_jobs: list[Job],
        *,
        subject_prefix: str,
        mode: str,
        source_errors: list[str] | None = None,
    ) -> list[str]:
        self.calls.append((yes_jobs, maybe_jobs, subject_prefix, mode, source_errors))
        return []


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        features=SimpleNamespace(notifications=True),
        filter=SimpleNamespace(require_us_location=False),
    )


def _job(key: str, title: str, label: str) -> Job:
    return Job(
        key=key,
        source="boards",
        company="ExampleCo",
        title=title,
        location="New York, NY",
        url=f"https://example.com/{key}",
        label=label,
        description="Example job description",
    )


class NotificationPolicyTests(unittest.TestCase):
    def test_notify_yes_only_skips_maybe_only_alerts(self) -> None:
        db = _FakeDatabase()
        notifier = _FakeNotifier()
        job = _job("maybe-1", "Maybe Role", "maybe")
        db.new_keys.add(job.key)

        with patch("src.main.evaluate_job", return_value=_FakeEvaluation(score=72, label="maybe")):
            _dispatch_results(
                all_jobs=[job],
                errors=[],
                db=db,
                notifier=notifier,
                mode="boards",
                dry_run=True,
                no_notify=False,
                test_notify=False,
                cfg=_cfg(),
                notify_yes_only=True,
            )

        self.assertEqual(notifier.calls, [])

    def test_notify_yes_only_sends_yes_jobs_without_maybe_jobs(self) -> None:
        db = _FakeDatabase()
        notifier = _FakeNotifier()
        yes_job = _job("yes-1", "Yes Role", "yes")
        maybe_job = _job("maybe-1", "Maybe Role", "maybe")
        db.new_keys.update({yes_job.key, maybe_job.key})

        def fake_evaluate_job(title: str, description: str, **_: object) -> _FakeEvaluation:
            if title == "Yes Role":
                return _FakeEvaluation(score=91, label="yes")
            return _FakeEvaluation(score=72, label="maybe")

        with patch("src.main.evaluate_job", side_effect=fake_evaluate_job):
            _dispatch_results(
                all_jobs=[yes_job, maybe_job],
                errors=[],
                db=db,
                notifier=notifier,
                mode="boards",
                dry_run=True,
                no_notify=False,
                test_notify=False,
                cfg=_cfg(),
                notify_yes_only=True,
            )

        self.assertEqual(len(notifier.calls), 1)
        yes_jobs, maybe_jobs, subject_prefix, mode, source_errors = notifier.calls[0]
        self.assertEqual([job.key for job in yes_jobs], ["yes-1"])
        self.assertEqual([job.key for job in maybe_jobs], ["maybe-1"])
        self.assertEqual(subject_prefix, "[Job Radar]")
        self.assertEqual(mode, "boards")
        self.assertEqual(source_errors, [])


if __name__ == "__main__":
    unittest.main()
