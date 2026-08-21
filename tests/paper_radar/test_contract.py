"""contract 모듈: 해시 가능성(Fetch), 검증 규칙(SourcePolicy), 편의 메서드."""

import unittest

from paper_radar.contract import Fetch, Payload, Source, SourcePolicy, Yield
from paper_radar.transport.errors import ParseError


class FetchHashabilityTest(unittest.TestCase):
    def test_fetch_can_be_placed_in_a_set(self):
        """frontier(수집 대상 큐)가 set 으로 중복 제거하려면 Fetch 가 해시 가능해야 한다."""
        fetch = Fetch(url="https://api.example.org/works/1")
        seen = {fetch}
        self.assertIn(fetch, seen)

    def test_two_equal_fetches_deduplicate_in_a_set(self):
        """같은 값의 Fetch 두 개는 set 에서 하나로 합쳐져야 dedup 의 의미가 있다."""
        a = Fetch(url="https://api.example.org/works/1", params=(("filter", "x"),))
        b = Fetch(url="https://api.example.org/works/1", params=(("filter", "x"),))
        self.assertEqual(len({a, b}), 1)

    def test_fetches_with_different_params_are_distinct_in_a_set(self):
        """params 가 다르면 다른 요청이므로 set 에서 각각 살아남아야 한다."""
        a = Fetch(url="https://api.example.org/works/1", params=(("cursor", "1"),))
        b = Fetch(url="https://api.example.org/works/1", params=(("cursor", "2"),))
        self.assertEqual(len({a, b}), 2)


class FetchCtxTest(unittest.TestCase):
    def test_ctx_returns_the_matching_context_value(self):
        """소스가 parse() 때 되찾을 자유 필드를 context 에서 꺼낼 수 있어야 한다."""
        fetch = Fetch(url="https://x", context=(("nct_id", "NCT001"),))
        self.assertEqual(fetch.ctx("nct_id"), "NCT001")

    def test_ctx_returns_default_when_key_is_absent(self):
        """존재하지 않는 키는 예외가 아니라 default 를 돌려줘야 편의 메서드다."""
        fetch = Fetch(url="https://x")
        self.assertIsNone(fetch.ctx("missing"))
        self.assertEqual(fetch.ctx("missing", "fallback"), "fallback")


class SourcePolicyValidationTest(unittest.TestCase):
    def test_accepts_a_minimal_valid_policy(self):
        """host 와 min_interval_s 만으로도 유효한 정책이어야 한다."""
        policy = SourcePolicy(host="api.example.org", min_interval_s=0.1)
        self.assertEqual(policy.host, "api.example.org")
        self.assertEqual(policy.max_attempts, 5)

    def test_rejects_negative_min_interval(self):
        """음수 간격은 과거로 sleep 하는 셈이라 페이스 제한이 무의미해진다."""
        with self.assertRaises(ValueError):
            SourcePolicy(host="api.example.org", min_interval_s=-0.1)

    def test_accepts_zero_min_interval(self):
        """0 은 유효하다 — 경계값이 배제되면 안 된다."""
        policy = SourcePolicy(host="api.example.org", min_interval_s=0.0)
        self.assertEqual(policy.min_interval_s, 0.0)

    def test_rejects_max_attempts_below_one(self):
        """0 번 시도는 항상 실패로 끝나므로 정책으로 허용해서는 안 된다."""
        with self.assertRaises(ValueError):
            SourcePolicy(host="api.example.org", min_interval_s=0.1, max_attempts=0)

    def test_rejects_auth_kind_without_auth_env_and_auth_name(self):
        """auth_kind 만 있고 env/name 이 없으면 transport 가 값을 어디서 읽어
        어디에 넣을지 알 수 없다 — 셋은 세트다."""
        with self.assertRaises(ValueError):
            SourcePolicy(host="api.example.org", min_interval_s=0.1, auth_kind="param")

    def test_accepts_auth_kind_with_env_and_name_together(self):
        """셋을 함께 주면 유효하다."""
        policy = SourcePolicy(
            host="api.example.org",
            min_interval_s=0.1,
            auth_kind="param",
            auth_env="EXAMPLE_API_KEY",
            auth_name="api_key",
        )
        self.assertEqual(policy.auth_name, "api_key")


class PayloadTest(unittest.TestCase):
    def _payload(self, body=b'{"ok": true}', status=200):
        return Payload(
            fetch=Fetch(url="https://x"),
            status=status,
            body=body,
            headers=(("content-type", "application/json"),),
            elapsed_ms=12,
            captured_at="2026-08-21T00:00:00+00:00",
        )

    def test_text_decodes_utf8_body(self):
        payload = self._payload(body="한글".encode())
        self.assertEqual(payload.text(), "한글")

    def test_text_replaces_undecodable_bytes_instead_of_raising(self):
        """대용량 논문 API 응답이 깨진 바이트를 섞어 보내도 크래시해서는 안 된다."""
        payload = self._payload(body=b"\xff\xfe not utf-8")
        self.assertIn("�", payload.text())

    def test_json_data_parses_valid_json(self):
        payload = self._payload(body=b'{"a": 1}')
        self.assertEqual(payload.json_data(), {"a": 1})

    def test_json_data_raises_parse_error_instead_of_returning_none(self):
        """200 인데 JSON 이 아니면 업스트림 형태 변경 신호다 — None 으로 숨기지 않는다."""
        payload = self._payload(body=b"<html>not json</html>")
        with self.assertRaises(ParseError):
            payload.json_data()

    def test_header_looks_up_by_normalized_lowercase_key(self):
        payload = self._payload()
        self.assertEqual(payload.header("content-type"), "application/json")

    def test_header_returns_default_when_absent(self):
        payload = self._payload()
        self.assertIsNone(payload.header("x-missing"))
        self.assertEqual(payload.header("x-missing", "d"), "d")


class YieldTest(unittest.TestCase):
    def test_follow_defaults_to_an_empty_tuple(self):
        """후속 요청이 없는 평범한 페이지는 follow=() 로 끝나야 한다."""
        result = Yield(records=(1, 2))
        self.assertEqual(result.follow, ())

    def test_can_carry_follow_up_fetches_for_pagination(self):
        follow_fetch = Fetch(url="https://x?cursor=2")
        result = Yield(records=(), follow=(follow_fetch,))
        self.assertEqual(result.follow, (follow_fetch,))


class SourceProtocolTest(unittest.TestCase):
    def test_class_with_key_and_policy_satisfies_the_protocol(self):
        class ExampleSource:
            key = "example"
            policy = SourcePolicy(host="api.example.org", min_interval_s=0.1)

        self.assertIsInstance(ExampleSource(), Source)

    def test_class_missing_policy_does_not_satisfy_the_protocol(self):
        class IncompleteSource:
            key = "incomplete"

        self.assertNotIsInstance(IncompleteSource(), Source)


if __name__ == "__main__":
    unittest.main()
