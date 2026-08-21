"""tool/fetch_cosing.py 의 순수 부분(sha256_of/header_row_of/build_meta) 단위
테스트. 네트워크가 있는 fetch()/main() 은 이 태스크에서 테스트하지 않는다
(브리핑 지시: "다운로드 자체는 테스트하지 않는다") — 코드 리뷰로만 검증한다.

tool/fetch_cosing.py 는 pytest 가 discover 하지 않는 위치에 있다(testpaths=
tests/ 바깥, tool/live_smoke.py 와 같은 배치) — importlib 로 파일 경로를 직접
불러온다(test_guards.py 의 importlib.import_module() 과 달리, tool/ 은
paper_radar 패키지 밖이라 일반 import 경로가 없다).
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "fetch_cosing", _REPO_ROOT / "tool" / "fetch_cosing.py"
)
fetch_cosing = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fetch_cosing)


class Sha256OfTest(unittest.TestCase):
    """순수 함수 — 디스크 읽기만, 네트워크 없음."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp()
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def test_matches_the_hashlib_reference_digest(self):
        content = b"COSING Ref No,INCI name\n1,WATER\n"
        Path(self.path).write_bytes(content)
        self.assertEqual(
            fetch_cosing.sha256_of(Path(self.path)), hashlib.sha256(content).hexdigest()
        )

    def test_differs_when_the_file_content_differs(self):
        Path(self.path).write_bytes(b"a")
        first = fetch_cosing.sha256_of(Path(self.path))
        Path(self.path).write_bytes(b"b")
        second = fetch_cosing.sha256_of(Path(self.path))
        self.assertNotEqual(first, second)


class HeaderRowOfTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp()
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def test_reads_the_first_line_with_the_bom_stripped(self):
        with open(self.path, "w", encoding="utf-8-sig", newline="") as handle:
            handle.write("COSING Ref No,INCI name\r\n1,WATER\r\n")
        header = fetch_cosing.header_row_of(Path(self.path))
        self.assertEqual(header, "COSING Ref No,INCI name")
        self.assertFalse(header.startswith("﻿"), "utf-8-sig 가 BOM 을 걷어내야 한다")

    def test_strips_the_trailing_newline(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("a,b,c\n1,2,3\n")
        self.assertEqual(fetch_cosing.header_row_of(Path(self.path)), "a,b,c")


class BuildMetaTest(unittest.TestCase):
    """build_meta() — sha256/size/header 계산을 조립한다. 순수 함수(fetched_at 은
    호출자가 넘긴다 — 이 함수 자체는 "지금" 을 모른다)."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp()
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def test_builds_url_sha256_size_bytes_and_header_row(self):
        content = "COSING Ref No,INCI name\n1,WATER\n"
        Path(self.path).write_text(content, encoding="utf-8")

        meta = fetch_cosing.build_meta(
            url="https://ec.europa.eu/cosing/export.csv",
            csv_path=Path(self.path),
            fetched_at="2026-08-21T00:00:00Z",
        )

        self.assertEqual(meta["url"], "https://ec.europa.eu/cosing/export.csv")
        self.assertEqual(meta["fetched_at"], "2026-08-21T00:00:00Z")
        self.assertEqual(meta["size_bytes"], len(content.encode("utf-8")))
        self.assertEqual(meta["header_row"], "COSING Ref No,INCI name")
        self.assertEqual(meta["sha256"], fetch_cosing.sha256_of(Path(self.path)))

    def test_does_not_hard_code_the_url_it_just_echoes_the_argument(self):
        # 브리핑: "URL 을 코드에 박지 않는다" — build_meta() 는 넘겨받은 url
        # 을 그대로 담을 뿐, 어떤 URL 상수도 참조하지 않는다는 것을 두 번
        # 다른 값으로 호출해 확인한다.
        Path(self.path).write_text("a,b\n1,2\n", encoding="utf-8")
        meta_a = fetch_cosing.build_meta(
            url="https://a.example/x.csv", csv_path=Path(self.path), fetched_at="t"
        )
        meta_b = fetch_cosing.build_meta(
            url="https://b.example/y.csv", csv_path=Path(self.path), fetched_at="t"
        )
        self.assertEqual(meta_a["url"], "https://a.example/x.csv")
        self.assertEqual(meta_b["url"], "https://b.example/y.csv")


if __name__ == "__main__":
    unittest.main()
