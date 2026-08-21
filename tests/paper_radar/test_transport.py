"""transport.http.Transport: 페이스, 재시도, 오류 타입, 인증 주입, captured_at 스탬프.

papers/tests/test_http.py 의 FakeSession/FakeResponse 페이크 패턴을 그대로
따르되, Transport 는 session.request(method, url, params, headers, data,
timeout) 단일 진입점을 쓰므로 페이크도 그 시그니처에 맞춘다. 실제 sleep 도
실제 clock 도 쓰지 않는다 — 둘 다 주입해서 테스트가 즉시 끝난다.
"""

import unittest
from datetime import UTC, datetime
from unittest import mock

import requests

from paper_radar.contract import Fetch, SourcePolicy
from paper_radar.transport import http
from paper_radar.transport.errors import (
    BudgetExhausted,
    NotFound,
    ParseError,
    PermanentError,
    RateLimited,
    TransientError,
)
from paper_radar.transport.http import Transport


class FakeResponse:
    def __init__(self, status_code, headers=None, body=b"{}"):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = body


class FakeSession:
    """호출 순서대로 응답을 돌려주는 세션. 요청 인자를 기록한다."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, params=None, headers=None, data=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "headers": headers,
                "data": data,
                "timeout": timeout,
            }
        )
        if not self._responses:
            raise AssertionError("예상보다 많이 호출되었습니다")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_clock_and_sleep(start=0.0):
    """fake clock 과 fake sleep 을 한 쌍으로 만든다. sleep 은 clock 을 실제로 전진시켜서
    다음 _throttle 계산이 일관되게 맞물리게 한다 — 진짜로 기다리지는 않는다.
    """
    state = {"now": start}
    calls = []

    def clock():
        return state["now"]

    def sleep(seconds):
        calls.append(seconds)
        state["now"] += seconds

    return clock, sleep, calls


DEFAULT_POLICY = SourcePolicy(host="api.example.org", min_interval_s=0.0)


class PacingTest(unittest.TestCase):
    def test_sleeps_when_second_call_is_faster_than_min_interval(self):
        """같은 host 로의 두 번째 호출이 min_interval_s 안에 들어오면 sleep 해야 한다."""
        clock, sleep, sleeps = make_clock_and_sleep()
        session = FakeSession([FakeResponse(200), FakeResponse(200)])
        transport = Transport(session=session, clock=clock, sleep=sleep)
        policy = SourcePolicy(host="api.example.org", min_interval_s=1.0)
        fetch = Fetch(url="https://api.example.org/x")

        transport.request(fetch, policy)
        transport.request(fetch, policy)

        self.assertIn(1.0, sleeps)

    def test_does_not_sleep_when_interval_already_elapsed(self):
        """min_interval_s 가 이미 지났다면 페이스 제한으로 인한 sleep 이 없어야 한다."""
        clock, sleep, sleeps = make_clock_and_sleep()
        session = FakeSession([FakeResponse(200), FakeResponse(200)])
        transport = Transport(session=session, clock=clock, sleep=sleep)
        policy = SourcePolicy(host="api.example.org", min_interval_s=1.0)
        fetch = Fetch(url="https://api.example.org/x")

        transport.request(fetch, policy)
        # sleep 을 거치지 않고 "2초 전에 호출했다"는 상태를 직접 만든다
        transport._last_call["api.example.org"] -= 2.0
        transport.request(fetch, policy)

        self.assertEqual(sleeps, [])


class RetryTest(unittest.TestCase):
    def test_retries_on_429_with_exponential_backoff_then_succeeds(self):
        """429 는 재시도 대상이고, 백오프는 1.5 * 2**attempt 여야 한다."""
        clock, sleep, sleeps = make_clock_and_sleep()
        session = FakeSession([FakeResponse(429), FakeResponse(200, body=b'{"ok": true}')])
        transport = Transport(session=session, clock=clock, sleep=sleep)

        payload = transport.request(Fetch(url="https://api.example.org/x"), DEFAULT_POLICY)

        self.assertEqual(payload.status, 200)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(sleeps, [1.5])

    def test_backoff_alone_covers_pacing_when_it_exceeds_min_interval(self):
        """min_interval_s(1.0초) 보다 백오프(1.5초)가 더 크면, 재시도 직전에 이미
        min_interval_s 이상 지난 셈이라 _throttle 이 추가로 sleep 을 넣으면 안 된다
        — 스로틀과 백오프가 겹쳐 이중으로 기다리면 안 된다."""
        clock, sleep, sleeps = make_clock_and_sleep()
        session = FakeSession([FakeResponse(429), FakeResponse(200)])
        transport = Transport(session=session, clock=clock, sleep=sleep)
        policy = SourcePolicy(host="api.example.org", min_interval_s=1.0)

        transport.request(Fetch(url="https://api.example.org/x"), policy)

        # 백오프(1.5초) 하나만 sleep 되고, 두 번째 시도 전 _throttle 이 추가로
        # sleep 을 넣지 않는다 — 1.5초 >= min_interval_s(1.0초) 이기 때문이다.
        self.assertEqual(sleeps, [1.5])

    def test_throttle_tops_up_the_remaining_wait_when_min_interval_exceeds_backoff(self):
        """min_interval_s(5.0초) 가 백오프(1.5초)보다 크면, 백오프로 기다린 1.5초는
        이미 흐른 시간으로 인정하고 _throttle 이 남은 3.5초만 추가로 sleep 해야 한다
        — 5.0초를 통째로 다시 기다리면 이중 대기(버그)다."""
        clock, sleep, sleeps = make_clock_and_sleep()
        session = FakeSession([FakeResponse(429), FakeResponse(200)])
        transport = Transport(session=session, clock=clock, sleep=sleep)
        policy = SourcePolicy(host="api.example.org", min_interval_s=5.0)

        transport.request(Fetch(url="https://api.example.org/x"), policy)

        self.assertEqual(sleeps, [1.5, 3.5])
        self.assertAlmostEqual(sum(sleeps), 5.0)

    def test_numeric_retry_after_header_overrides_backoff(self):
        """Retry-After 가 숫자형이면 지수 백오프(1.5초)보다 그 값(5초)을 써야 한다."""
        clock, sleep, sleeps = make_clock_and_sleep()
        session = FakeSession(
            [FakeResponse(429, headers={"Retry-After": "5"}), FakeResponse(200)]
        )
        transport = Transport(session=session, clock=clock, sleep=sleep)

        transport.request(Fetch(url="https://api.example.org/x"), DEFAULT_POLICY)

        self.assertEqual(sleeps, [5.0])

    def test_http_date_retry_after_header_is_parsed(self):
        """기존 papers/http.py 는 HTTP-date 형 Retry-After 를 버렸다. 이번에는
        email.utils.parsedate_to_datetime 으로 파싱해 초 단위 지연으로 바꿔야 한다."""
        fixed_now = datetime(2026, 8, 21, 0, 0, 0, tzinfo=UTC)
        clock, sleep, sleeps = make_clock_and_sleep()
        session = FakeSession(
            [
                FakeResponse(429, headers={"Retry-After": "Fri, 21 Aug 2026 00:00:10 GMT"}),
                FakeResponse(200),
            ]
        )
        transport = Transport(session=session, clock=clock, sleep=sleep)

        with mock.patch.object(http, "_utcnow", return_value=fixed_now):
            transport.request(Fetch(url="https://api.example.org/x"), DEFAULT_POLICY)

        self.assertEqual(sleeps, [10.0])

    def test_raises_rate_limited_when_429_retries_are_exhausted(self):
        """429 가 max_attempts 만큼 반복되면 RateLimited 로 포기해야 한다."""
        policy = SourcePolicy(host="api.example.org", min_interval_s=0.0, max_attempts=3)
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession([FakeResponse(429) for _ in range(3)])
        transport = Transport(session=session, clock=clock, sleep=sleep)

        with self.assertRaises(RateLimited):
            transport.request(Fetch(url="https://api.example.org/x"), policy)
        self.assertEqual(len(session.calls), 3)

    def test_raises_transient_error_when_5xx_retries_are_exhausted(self):
        """5xx 가 max_attempts 만큼 반복되면 TransientError 로 포기해야 한다."""
        policy = SourcePolicy(host="api.example.org", min_interval_s=0.0, max_attempts=3)
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession([FakeResponse(503) for _ in range(3)])
        transport = Transport(session=session, clock=clock, sleep=sleep)

        with self.assertRaises(TransientError):
            transport.request(Fetch(url="https://api.example.org/x"), policy)
        self.assertEqual(len(session.calls), 3)

    def test_survives_connection_exception_and_retries(self):
        """연결 예외는 즉시 죽지 않고 재시도 대상으로 취급해야 한다."""
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession([requests.ConnectionError("연결 끊김"), FakeResponse(200)])
        transport = Transport(session=session, clock=clock, sleep=sleep)

        payload = transport.request(Fetch(url="https://api.example.org/x"), DEFAULT_POLICY)

        self.assertEqual(payload.status, 200)

    def test_raises_transient_error_when_connection_exceptions_are_exhausted(self):
        """연결 예외가 계속되면 결국 TransientError 로 포기해야 한다."""
        policy = SourcePolicy(host="api.example.org", min_interval_s=0.0, max_attempts=2)
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession([requests.Timeout("타임아웃"), requests.Timeout("타임아웃")])
        transport = Transport(session=session, clock=clock, sleep=sleep)

        with self.assertRaises(TransientError):
            transport.request(Fetch(url="https://api.example.org/x"), policy)


class ErrorTypeTest(unittest.TestCase):
    def _transport(self, responses):
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession(responses)
        return Transport(session=session, clock=clock, sleep=sleep), session

    def test_404_raises_not_found(self):
        """404 는 '그 레코드는 없다'는 정상 결과의 하나 — NotFound 로 명확히 구분한다."""
        transport, session = self._transport([FakeResponse(404)])
        with self.assertRaises(NotFound):
            transport.request(Fetch(url="https://api.example.org/x"), DEFAULT_POLICY)
        self.assertEqual(len(session.calls), 1)

    def test_402_raises_budget_exhausted_without_retrying(self):
        """402 는 일일 예산 소진 추정 — 재시도해도 무의미하므로 즉시 포기해야 한다."""
        transport, session = self._transport([FakeResponse(402)])
        with self.assertRaises(BudgetExhausted):
            transport.request(Fetch(url="https://api.example.org/x"), DEFAULT_POLICY)
        self.assertEqual(len(session.calls), 1)

    def test_409_raises_budget_exhausted_without_retrying(self):
        transport, session = self._transport([FakeResponse(409)])
        with self.assertRaises(BudgetExhausted):
            transport.request(Fetch(url="https://api.example.org/x"), DEFAULT_POLICY)
        self.assertEqual(len(session.calls), 1)

    def test_other_4xx_raises_permanent_error(self):
        """404/402/409 를 제외한 4xx 는 요청 자체가 잘못됐다는 뜻 — 재시도하지 않는다."""
        transport, session = self._transport([FakeResponse(418)])
        with self.assertRaises(PermanentError):
            transport.request(Fetch(url="https://api.example.org/x"), DEFAULT_POLICY)
        self.assertEqual(len(session.calls), 1)

    def test_5xx_outside_retry_status_raises_transient_error_without_retrying(self):
        """501 은 RETRY_STATUS 밖이라 재시도하지는 않지만, 서버측 오류라는 사실은
        같으므로 PermanentError(영구 포기) 가 아니라 TransientError(다음 실행에서
        재시도 가능)로 던져야 한다. 이걸 PermanentError 로 잘못 분류하면 호출자가
        일시적 서버 장애를 영구 실패로 취급해 재시도 기회를 영영 잃는다."""
        transport, session = self._transport([FakeResponse(501)])
        with self.assertRaises(TransientError):
            transport.request(Fetch(url="https://api.example.org/x"), DEFAULT_POLICY)
        self.assertEqual(len(session.calls), 1)

    def test_json_data_raises_parse_error_on_malformed_body(self):
        """200 인데 JSON 이 아니면 get_json() 이 None 이 아니라 ParseError 를 던져야 한다."""
        transport, _ = self._transport([FakeResponse(200, body=b"<html>not json</html>")])
        with self.assertRaises(ParseError):
            transport.get_json("https://api.example.org/x", policy=DEFAULT_POLICY)

    def test_budget_status_does_not_overlap_retry_status(self):
        """402/409 는 재시도 없이 즉시 포기하고 429/5xx 는 재시도한다 — 겹치면 둘 중
        하나의 분기가 죽은 코드가 된다."""
        self.assertEqual(http.BUDGET_STATUS & http.RETRY_STATUS, frozenset())


class BudgetObservationTest(unittest.TestCase):
    def test_observes_budget_headers_even_on_an_error_response(self):
        """오류 응답이라도 예산 헤더가 실려 오면 기록해야 한다 — 소진 직전에 알아야
        다음 실행에서 예산을 아낄 수 있다."""
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession([FakeResponse(404, headers={"x-ratelimit-remaining": "42"})])
        transport = Transport(session=session, clock=clock, sleep=sleep)

        with self.assertRaises(NotFound):
            transport.request(Fetch(url="https://api.example.org/x"), DEFAULT_POLICY)

        self.assertEqual(transport.budget.remaining("api.example.org"), 42)


class AuthInjectionTest(unittest.TestCase):
    def test_injects_credential_as_a_query_param_when_auth_kind_is_param(self):
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession([FakeResponse(200)])
        transport = Transport(session=session, clock=clock, sleep=sleep)
        policy = SourcePolicy(
            host="api.example.org",
            min_interval_s=0.0,
            auth_kind="param",
            auth_env="EXAMPLE_API_KEY",
            auth_name="api_key",
        )

        with mock.patch.dict("os.environ", {"EXAMPLE_API_KEY": "secret"}, clear=False):
            transport.request(Fetch(url="https://api.example.org/x"), policy)

        self.assertEqual(session.calls[0]["params"]["api_key"], "secret")

    def test_injects_credential_as_a_header_when_auth_kind_is_header(self):
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession([FakeResponse(200)])
        transport = Transport(session=session, clock=clock, sleep=sleep)
        policy = SourcePolicy(
            host="api.example.org",
            min_interval_s=0.0,
            auth_kind="header",
            auth_env="EXAMPLE_API_KEY",
            auth_name="x-api-key",
        )

        with mock.patch.dict("os.environ", {"EXAMPLE_API_KEY": "secret"}, clear=False):
            transport.request(Fetch(url="https://api.example.org/x"), policy)

        self.assertEqual(session.calls[0]["headers"]["x-api-key"], "secret")

    def test_omits_credential_when_env_var_is_absent(self):
        """키가 없어도 조용히 생략하고 동작해야 한다 — 크래시하지 않는다."""
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession([FakeResponse(200)])
        transport = Transport(session=session, clock=clock, sleep=sleep)
        policy = SourcePolicy(
            host="api.example.org",
            min_interval_s=0.0,
            auth_kind="param",
            auth_env="MISSING_API_KEY",
            auth_name="api_key",
        )

        with mock.patch.dict("os.environ", {}, clear=False):
            import os as _os

            _os.environ.pop("MISSING_API_KEY", None)
            transport.request(Fetch(url="https://api.example.org/x"), policy)

        self.assertNotIn("api_key", session.calls[0]["params"])


class ObserverTest(unittest.TestCase):
    """Transport(observer=...) 훅 — T5b 가 fetch_log 를 기록하는 데 쓴다."""

    def test_calls_observer_once_on_a_single_successful_attempt(self):
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession([FakeResponse(200)])
        calls = []
        transport = Transport(
            session=session,
            clock=clock,
            sleep=sleep,
            observer=lambda fetch, status, attempt, elapsed_ms, error: calls.append(
                (status, attempt, error)
            ),
        )

        transport.request(Fetch(url="https://api.example.org/x"), DEFAULT_POLICY)

        self.assertEqual(calls, [(200, 0, None)])

    def test_calls_observer_once_per_attempt_on_retry(self):
        """재시도가 있으면(429 -> 200) 시도마다 정확히 1회씩, 총 2회 불려야 한다."""
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession([FakeResponse(429), FakeResponse(200)])
        calls = []
        transport = Transport(
            session=session,
            clock=clock,
            sleep=sleep,
            observer=lambda fetch, status, attempt, elapsed_ms, error: calls.append(
                (status, attempt, error)
            ),
        )

        transport.request(Fetch(url="https://api.example.org/x"), DEFAULT_POLICY)

        self.assertEqual(calls, [(429, 0, None), (200, 1, None)])

    def test_passes_none_status_and_the_error_text_on_a_connection_failure(self):
        """전송 자체가 실패하면 status=None, error_str 에 예외 메시지가 담겨야 한다."""
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession([requests.ConnectionError("연결 끊김"), FakeResponse(200)])
        calls = []
        transport = Transport(
            session=session,
            clock=clock,
            sleep=sleep,
            observer=lambda fetch, status, attempt, elapsed_ms, error: calls.append(
                (status, attempt, error)
            ),
        )

        transport.request(Fetch(url="https://api.example.org/x"), DEFAULT_POLICY)

        self.assertEqual(calls[0][0], None)
        self.assertEqual(calls[0][1], 0)
        self.assertIn("연결 끊김", calls[0][2])
        self.assertEqual(calls[1], (200, 1, None))

    def test_observer_exception_does_not_affect_the_request_result(self):
        """observer 자신이 던진 예외는 삼켜야 한다 — 관측 코드의 버그가 수집
        결과에 영향을 주면 안 된다."""
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession([FakeResponse(200, body=b'{"ok": true}')])

        def bad_observer(fetch, status, attempt, elapsed_ms, error):
            raise RuntimeError("observer 고장")

        transport = Transport(session=session, clock=clock, sleep=sleep, observer=bad_observer)

        payload = transport.request(Fetch(url="https://api.example.org/x"), DEFAULT_POLICY)

        self.assertEqual(payload.status, 200)
        self.assertEqual(payload.json_data(), {"ok": True})

    def test_set_observer_wires_up_a_hook_installed_after_construction(self):
        """T5b 의 collect() 는 RunLog.start() 로 run_id 를 받은 뒤에야 observer 를
        만들 수 있다 — 생성자가 아니라 set_observer() 로 나중에 붙일 수 있어야 한다."""
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession([FakeResponse(200)])
        transport = Transport(session=session, clock=clock, sleep=sleep)
        calls = []

        transport.set_observer(
            lambda fetch, status, attempt, elapsed_ms, error: calls.append((status, attempt))
        )
        transport.request(Fetch(url="https://api.example.org/x"), DEFAULT_POLICY)

        self.assertEqual(calls, [(200, 0)])

    def test_set_observer_returns_the_previous_observer_so_callers_can_restore_it(self):
        """collect() 처럼 observer 를 임시로 갈아 끼우는 호출자는 끝난 뒤
        원래 있던(또는 없던) observer 로 되돌려야 한다 — 그러려면 갈아 끼우는
        시점에 이전 값을 돌려받아야 한다. None 강제 초기화는 바깥 호출자가
        미리 걸어 둔 observer 를 지워버리므로 안전하지 않다."""
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession([FakeResponse(200), FakeResponse(200)])
        original_calls = []

        def original(fetch, status, attempt, elapsed_ms, error):
            original_calls.append(status)

        transport = Transport(session=session, clock=clock, sleep=sleep, observer=original)

        temporary_calls = []

        def temporary_observer(fetch, status, attempt, elapsed_ms, error):
            temporary_calls.append(status)

        previous = transport.set_observer(temporary_observer)
        self.assertIs(previous, original)  # 갈아 끼우기 전에 있던 observer 를 돌려받는다
        transport.request(Fetch(url="https://api.example.org/x"), DEFAULT_POLICY)
        self.assertEqual(temporary_calls, [200])
        self.assertEqual(original_calls, [])  # 임시 observer 로 교체된 동안은 안 불림

        restored = transport.set_observer(previous)
        self.assertIs(restored, temporary_observer)  # 이번엔 직전(임시) observer 를 돌려준다
        transport.request(Fetch(url="https://api.example.org/x"), DEFAULT_POLICY)
        self.assertEqual(original_calls, [200])  # 복원된 뒤에는 원래 observer 가 다시 불린다

    def test_set_observer_none_removes_a_previously_installed_hook(self):
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession([FakeResponse(200), FakeResponse(200)])
        calls = []
        transport = Transport(
            session=session,
            clock=clock,
            sleep=sleep,
            observer=lambda fetch, status, attempt, elapsed_ms, error: calls.append(status),
        )
        transport.request(Fetch(url="https://api.example.org/x"), DEFAULT_POLICY)

        transport.set_observer(None)
        transport.request(Fetch(url="https://api.example.org/x"), DEFAULT_POLICY)

        self.assertEqual(calls, [200])  # 두 번째 호출은 관측되지 않았다

    def test_no_observer_means_no_crash(self):
        """observer=None(기본값)이면 그냥 아무 일도 하지 않아야 한다."""
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession([FakeResponse(200)])
        transport = Transport(session=session, clock=clock, sleep=sleep)

        payload = transport.request(Fetch(url="https://api.example.org/x"), DEFAULT_POLICY)

        self.assertEqual(payload.status, 200)


class CapturedAtTest(unittest.TestCase):
    def test_captured_at_is_stamped_by_transport_not_the_caller(self):
        fixed_now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession([FakeResponse(200)])
        transport = Transport(session=session, clock=clock, sleep=sleep)

        with mock.patch.object(http, "_utcnow", return_value=fixed_now):
            payload = transport.request(Fetch(url="https://api.example.org/x"), DEFAULT_POLICY)

        self.assertEqual(payload.captured_at, fixed_now.isoformat())


if __name__ == "__main__":
    unittest.main()
