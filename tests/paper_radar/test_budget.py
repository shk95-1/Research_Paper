"""transport.budget.BudgetTracker: 헤더 파싱, remaining 조회, 임계 경고, snapshot."""

import contextlib
import io
import unittest

from paper_radar.transport.budget import WARN_THRESHOLD, BudgetTracker


class ObserveTest(unittest.TestCase):
    def setUp(self):
        self.tracker = BudgetTracker()
        self.stderr = io.StringIO()

    def test_updates_remaining_from_the_ratelimit_remaining_header(self):
        with contextlib.redirect_stderr(self.stderr):
            self.tracker.observe("api.openalex.org", {"x-ratelimit-remaining": "97600"})
        self.assertEqual(self.tracker.remaining("api.openalex.org"), 97600)

    def test_is_case_insensitive_about_header_names(self):
        """서버마다 헤더 대소문자 표기가 다르므로 대소문자 구분 없이 찾아야 한다."""
        with contextlib.redirect_stderr(self.stderr):
            self.tracker.observe("api.openalex.org", {"X-RateLimit-Remaining": "500"})
        self.assertEqual(self.tracker.remaining("api.openalex.org"), 500)

    def test_keeps_the_most_recent_value_across_multiple_observations(self):
        with contextlib.redirect_stderr(self.stderr):
            self.tracker.observe("api.openalex.org", {"x-ratelimit-remaining": "500"})
            self.tracker.observe("api.openalex.org", {"x-ratelimit-remaining": "400"})
        self.assertEqual(self.tracker.remaining("api.openalex.org"), 400)

    def test_ignores_headers_with_unparsable_values(self):
        """숫자로 못 바꾸는 값은 크래시가 아니라 조용히 무시해야 한다."""
        with contextlib.redirect_stderr(self.stderr):
            self.tracker.observe("api.openalex.org", {"x-ratelimit-remaining": "not-a-number"})
        self.assertIsNone(self.tracker.remaining("api.openalex.org"))

    def test_silently_ignores_a_response_with_no_relevant_headers(self):
        """예산제가 없는 소스는 이 헤더들을 아예 안 줄 수 있다 — 무시하는 것이 정상이다."""
        with contextlib.redirect_stderr(self.stderr):
            self.tracker.observe("www.ebi.ac.uk", {"content-type": "application/json"})
        self.assertIsNone(self.tracker.remaining("www.ebi.ac.uk"))
        self.assertEqual(self.stderr.getvalue(), "")


class RemainingTest(unittest.TestCase):
    def test_is_none_before_any_response_has_been_observed(self):
        tracker = BudgetTracker()
        self.assertIsNone(tracker.remaining("api.openalex.org"))


class WarnThresholdTest(unittest.TestCase):
    def setUp(self):
        self.tracker = BudgetTracker()
        self.stderr = io.StringIO()

    def test_warns_once_when_remaining_first_drops_below_the_threshold(self):
        with contextlib.redirect_stderr(self.stderr):
            self.tracker.observe(
                "api.openalex.org", {"x-ratelimit-remaining": str(WARN_THRESHOLD - 1)}
            )
        self.assertEqual(self.stderr.getvalue().count("남은 예산"), 1)

    def test_does_not_warn_again_on_a_further_drop_for_the_same_host(self):
        """스팸 방지 — 이미 경고했으면 더 떨어져도 다시 경고하지 않는다."""
        with contextlib.redirect_stderr(self.stderr):
            self.tracker.observe("api.openalex.org", {"x-ratelimit-remaining": "50"})
            self.tracker.observe("api.openalex.org", {"x-ratelimit-remaining": "10"})
        self.assertEqual(self.stderr.getvalue().count("남은 예산"), 1)

    def test_does_not_warn_while_remaining_stays_at_or_above_the_threshold(self):
        with contextlib.redirect_stderr(self.stderr):
            self.tracker.observe("api.openalex.org", {"x-ratelimit-remaining": str(WARN_THRESHOLD)})
        self.assertNotIn("남은 예산", self.stderr.getvalue())

    def test_warns_independently_per_host(self):
        """host 마다 별도로 추적해야 한다 — 한 host 의 경고가 다른 host 를 막으면 안 된다."""
        with contextlib.redirect_stderr(self.stderr):
            self.tracker.observe("api.openalex.org", {"x-ratelimit-remaining": "10"})
            self.tracker.observe("api.crossref.org", {"x-ratelimit-remaining": "10"})
        self.assertEqual(self.stderr.getvalue().count("남은 예산"), 2)


class SnapshotTest(unittest.TestCase):
    def test_snapshot_reflects_the_latest_observed_state_per_host(self):
        tracker = BudgetTracker()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            tracker.observe(
                "api.openalex.org",
                {
                    "x-ratelimit-remaining": "97600",
                    "x-ratelimit-limit": "100000",
                    "x-ratelimit-credits-used": "1",
                },
            )
        snapshot = tracker.snapshot()
        self.assertEqual(
            snapshot["api.openalex.org"],
            {"remaining": 97600, "credits_used": 1, "limit": 100000},
        )

    def test_snapshot_is_empty_before_any_observation(self):
        self.assertEqual(BudgetTracker().snapshot(), {})

    def test_snapshot_returns_a_copy_not_a_live_reference(self):
        """호출자가 snapshot 을 고쳐도 tracker 내부 상태에 영향을 주면 안 된다."""
        tracker = BudgetTracker()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            tracker.observe("api.openalex.org", {"x-ratelimit-remaining": "10"})
        snapshot = tracker.snapshot()
        snapshot["api.openalex.org"]["remaining"] = 0
        self.assertEqual(tracker.remaining("api.openalex.org"), 10)


if __name__ == "__main__":
    unittest.main()
