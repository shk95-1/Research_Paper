"""cache 모듈: 조회 결과 캐시. '없음'도 캐시해야 404 를 반복하지 않는다."""

import os
import tempfile
import unittest

from papers import cache, store


class CacheTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.path)
        self.conn = store.connect(self.path)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_miss_when_nothing_was_stored(self):
        self.assertIs(cache.get(self.conn, "crossref", "10.1/a"), cache.MISS)

    def test_round_trips_a_payload(self):
        cache.put(self.conn, "crossref", "10.1/a", {"title": "T"})
        self.assertEqual(cache.get(self.conn, "crossref", "10.1/a"), {"title": "T"})

    def test_distinguishes_a_cached_none_from_a_miss(self):
        # 404 를 캐시한 상태. 재조회하면 안 되므로 MISS 가 아니어야 한다.
        cache.put(self.conn, "crossref", "10.1/absent", None)
        self.assertIsNone(cache.get(self.conn, "crossref", "10.1/absent"))
        self.assertIsNot(cache.get(self.conn, "crossref", "10.1/absent"), cache.MISS)

    def test_separates_sources_sharing_a_key(self):
        cache.put(self.conn, "crossref", "10.1/a", {"from": "crossref"})
        cache.put(self.conn, "semantic_scholar", "10.1/a", {"from": "s2"})
        self.assertEqual(cache.get(self.conn, "crossref", "10.1/a"), {"from": "crossref"})
        self.assertEqual(cache.get(self.conn, "semantic_scholar", "10.1/a"), {"from": "s2"})

    def test_put_twice_overwrites_and_keeps_one_row(self):
        cache.put(self.conn, "crossref", "10.1/a", {"v": 1})
        cache.put(self.conn, "crossref", "10.1/a", {"v": 2})
        count = self.conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(cache.get(self.conn, "crossref", "10.1/a"), {"v": 2})

    def test_records_fetched_at(self):
        cache.put(self.conn, "crossref", "10.1/a", {"v": 1})
        row = self.conn.execute("SELECT fetched_at FROM cache").fetchone()
        self.assertTrue(row["fetched_at"].endswith("Z"))

    def test_empty_key_is_never_cached(self):
        # DOI 가 없는 논문은 캐시 키가 없다. 서로 다른 논문이 한 칸을 공유하면 안 된다.
        cache.put(self.conn, "crossref", "", {"v": 1})
        self.assertIs(cache.get(self.conn, "crossref", ""), cache.MISS)

    def test_fetch_calls_through_on_a_miss_and_stores_the_result(self):
        calls = []

        def loader():
            calls.append(1)
            return {"title": "T"}

        first = cache.fetch(self.conn, "crossref", "10.1/a", loader)
        second = cache.fetch(self.conn, "crossref", "10.1/a", loader)
        self.assertEqual(first, {"title": "T"})
        self.assertEqual(second, {"title": "T"})
        self.assertEqual(len(calls), 1, "두 번째 호출은 캐시에서 나와야 한다")

    def test_fetch_caches_a_none_result_too(self):
        calls = []

        def loader():
            calls.append(1)
            return None

        self.assertIsNone(cache.fetch(self.conn, "crossref", "10.1/a", loader))
        self.assertIsNone(cache.fetch(self.conn, "crossref", "10.1/a", loader))
        self.assertEqual(len(calls), 1, "미발견도 캐시해야 404 를 반복하지 않는다")

    def test_fetch_without_a_key_always_calls_through(self):
        calls = []

        def loader():
            calls.append(1)
            return {"v": 1}

        cache.fetch(self.conn, "europepmc", "", loader)
        cache.fetch(self.conn, "europepmc", "", loader)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
