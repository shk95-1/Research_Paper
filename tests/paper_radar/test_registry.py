"""registry: 정상 등록, key 중복, policy 계약 위반, get_source 오류 메시지.

SOURCES 는 모듈 전역이라 테스트끼리 오염될 수 있다 — 매 테스트에서 등록한
key 를 addCleanup 으로 반드시 지운다 (registry.py 자체는 trend-radar 교본의
패턴대로 전역 dict 를 쓰는 것이 의도된 설계다: 소스는 프로세스 생애주기
동안 한 번만 등록되면 되고, 여러 인스턴스가 필요 없다).
"""

import unittest

from paper_radar.contract import SourcePolicy
from paper_radar.registry import SOURCES, get_source, register


class RegisterTest(unittest.TestCase):
    def test_registers_a_valid_source_under_its_key(self):
        @register
        class ExampleSource:
            key = "example-register-valid"
            policy = SourcePolicy(host="api.example.org", min_interval_s=0.1)

        self.addCleanup(SOURCES.pop, "example-register-valid", None)
        self.assertIs(SOURCES["example-register-valid"], ExampleSource)

    def test_raises_on_duplicate_key(self):
        class First:
            key = "example-register-duplicate"
            policy = SourcePolicy(host="api.example.org", min_interval_s=0.1)

        register(First)
        self.addCleanup(SOURCES.pop, "example-register-duplicate", None)

        class Second:
            key = "example-register-duplicate"
            policy = SourcePolicy(host="api.other.org", min_interval_s=0.1)

        with self.assertRaises(ValueError):
            register(Second)
        # 중복 시도가 기존 등록을 덮어써서는 안 된다
        self.assertIs(SOURCES["example-register-duplicate"], First)

    def test_raises_when_key_is_empty(self):
        class EmptyKeySource:
            key = ""
            policy = SourcePolicy(host="api.example.org", min_interval_s=0.1)

        with self.assertRaises(ValueError):
            register(EmptyKeySource)

    def test_raises_when_key_is_missing(self):
        class NoKeySource:
            policy = SourcePolicy(host="api.example.org", min_interval_s=0.1)

        with self.assertRaises(ValueError):
            register(NoKeySource)

    def test_raises_when_policy_is_not_a_source_policy_instance(self):
        class BadPolicySource:
            key = "example-register-bad-policy"
            policy = {"host": "api.example.org"}  # dict 는 SourcePolicy 가 아니다

        with self.assertRaises(ValueError):
            register(BadPolicySource)
        self.assertNotIn("example-register-bad-policy", SOURCES)

    def test_raises_when_policy_is_missing(self):
        class NoPolicySource:
            key = "example-register-no-policy"

        with self.assertRaises(ValueError):
            register(NoPolicySource)


class GetSourceTest(unittest.TestCase):
    def test_returns_the_registered_class_for_a_known_key(self):
        @register
        class ExampleSource:
            key = "example-get-source-known"
            policy = SourcePolicy(host="api.example.org", min_interval_s=0.1)

        self.addCleanup(SOURCES.pop, "example-get-source-known", None)
        self.assertIs(get_source("example-get-source-known"), ExampleSource)

    def test_raises_key_error_with_known_keys_listed_for_an_unknown_key(self):
        @register
        class ExampleSource:
            key = "example-get-source-listed"
            policy = SourcePolicy(host="api.example.org", min_interval_s=0.1)

        self.addCleanup(SOURCES.pop, "example-get-source-listed", None)

        with self.assertRaises(KeyError) as ctx:
            get_source("no-such-source")

        message = str(ctx.exception)
        self.assertIn("no-such-source", message)
        self.assertIn("example-get-source-listed", message)


if __name__ == "__main__":
    unittest.main()
