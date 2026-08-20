"""pubmed 모듈: MeSH 확장을 쓰는 월별 계수 소스."""

import unittest
from unittest import mock

from papers.sources import pubmed


class MonthlyTrendTest(unittest.TestCase):
    def test_asks_once_per_month_and_reads_the_count(self):
        payloads = [{"esearchresult": {"count": str(n)}} for n in range(1, 13)]
        with mock.patch.object(pubmed.http, "get_json", side_effect=payloads) as get_json:
            counts = pubmed.monthly_trend("cosmetic", 2024, 2024)
        self.assertEqual(get_json.call_count, 12)
        self.assertEqual(counts[0], ("2024-01", 1))
        self.assertEqual(counts[-1], ("2024-12", 12))

    def test_bounds_each_month_on_its_real_last_day(self):
        payload = {"esearchresult": {"count": "0"}}
        with mock.patch.object(pubmed.http, "get_json", return_value=payload) as g:
            pubmed.monthly_trend("cosmetic", 2024, 2024)
        # 2024 는 윤년이다. 2월을 28일로 끊으면 하루치가 사라진다.
        self.assertIn("2024/02/29", g.call_args_list[1].kwargs["params"]["term"])

    def test_drops_a_month_it_could_not_ask_about(self):
        payloads = [{"esearchresult": {"count": "5"}}, pubmed.http.TRANSIENT] + [
            {"esearchresult": {"count": "5"}} for _ in range(10)
        ]
        with mock.patch.object(pubmed.http, "get_json", side_effect=payloads):
            counts = pubmed.monthly_trend("cosmetic", 2024, 2024)
        self.assertEqual(len(counts), 11)
        self.assertNotIn("2024-02", [period for period, _ in counts])

    def test_drops_a_month_whose_count_is_not_a_number(self):
        # 지어내느니 빠지는 편이 낫다. 0 으로 채우면 '없던 달'과 구별되지 않는다.
        payloads = [{"esearchresult": {"count": "nope"}}] + [
            {"esearchresult": {"count": "5"}} for _ in range(11)
        ]
        with mock.patch.object(pubmed.http, "get_json", side_effect=payloads):
            counts = pubmed.monthly_trend("cosmetic", 2024, 2024)
        self.assertEqual(len(counts), 11)

    def test_respects_the_ncbi_rate_limit(self):
        from papers import http
        # 키 없이 초당 3회. 이 값이 사라지면 NCBI 가 차단할 이유가 생긴다.
        self.assertGreaterEqual(http.MIN_INTERVAL["eutils.ncbi.nlm.nih.gov"], 1 / 3)
