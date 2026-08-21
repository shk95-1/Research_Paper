"""가드 테스트 — 계층 방향(AST), 소켓 금지 자체 검증, 소스 선언 정합성.

trend-radar 교본의 "패턴 3(계층 분리)"과 "패턴 7(자기기록)" 정신을 관례가
아니라 테스트로 강제하는 T7 의 마지막 조각이다. 여기서 강제하는 규칙은
전부 T3~T6 리뷰·설계 문서가 이미 관례로 확정한 것들이고, 이 파일은 그
관례가 조용히 깨지는 것을 막는다.
"""

from __future__ import annotations

import ast
import importlib
import socket
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from paper_radar import registry

# 등록 부작용 트리거 — registry.SOURCES 를 검사하려면 7개 소스 모듈(T10 의
# pubmed, T11 의 clinicaltrials 포함)이 먼저 import 되어 @register 데코레이터가
# 실행돼야 한다.
from paper_radar.sources import (  # noqa: F401
    clinicaltrials,
    crossref,
    europepmc,
    openalex,
    pubmed,
    semantic_scholar,
    unpaywall,
)

# test_guards.py 는 tests/paper_radar/ 아래 있다 — parents[0]=paper_radar(tests),
# [1]=tests, [2]=저장소 루트. cli.py 의 DEFAULT_DB 계산(parents[2], cli.py 는
# src/paper_radar 바로 아래)과 같은 깊이라 같은 규칙으로 저장소 루트에 닿는다.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src" / "paper_radar"


def _iter_py_files():
    """src/paper_radar/ 아래 모든 .py 파일(캐시 제외)을 경로 정렬 순으로."""
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _module_name(path: Path) -> str:
    """파일 경로를 점(dot) 모듈 이름으로. __init__.py 는 패키지 자체로 접는다."""
    rel = path.relative_to(_SRC_ROOT.parent)  # "paper_radar/sources/openalex.py"
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _layer_of(module_name: str) -> str:
    """모듈 이름에서 계층 이름을 뽑는다. sources/storage/evidence/trend/trials/
    transport 만 고유 계층으로 취급하고, 그 외(cli/__init__/__main__/contract/
    models/registry)는 "top"(계층 규칙의 대상이 아닌 조립·계약 계층)으로
    묶는다. trials(T11)는 evidence/trend 와 대칭인 세 번째 독립 파이프라인
    이다 — trial 테이블이라는 완전히 별개의 저장 표면을 쓴다."""
    parts = module_name.split(".")
    if len(parts) < 2:
        return "top"
    if parts[1] in {"sources", "storage", "evidence", "trend", "trials", "transport"}:
        return parts[1]
    return "top"


def _imports_of(path: Path) -> list[str]:
    """파일 안에서 'paper_radar.' 로 시작하는 import 대상을 점 경로 문자열로 모은다.

    `from paper_radar.sources import crossref` 같은 from-import 는 `crossref`
    가 서브모듈인지 심볼인지 AST 만으로는 알 수 없어, 안전하게 module.name
    형태(예: "paper_radar.sources.crossref")로 합성한다 — 계층 검사는 접두사
    비교라 심볼/서브모듈 구분이 결과에 영향을 주지 않는다.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "paper_radar" or alias.name.startswith("paper_radar."):
                    targets.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue  # 상대 import — 이 저장소 관례상 없어야 정상이라 방어적으로만 무시
            if node.module == "paper_radar" or node.module.startswith("paper_radar."):
                for alias in node.names:
                    targets.append(f"{node.module}.{alias.name}")
    return targets


def _starts_with_any(target: str, prefixes: tuple[str, ...]) -> bool:
    return any(target == prefix or target.startswith(prefix + ".") for prefix in prefixes)


# sources/*: T5a 실측으로는 contract(SourcePolicy)와 registry(@register)만
# 쓴다. transport.errors 는 소스가 앞으로 직접 예외 타입을 잡아야 할 필요에
# 대비해 허용 집합에 셋으로 남겨 둔다(브리핑 지시) — 지금 실제로 쓰는 곳은
# 없지만 계약상 열어 둔다. paper_radar.sources 자기 자신은 sources/__init__.py
# 가 5개 서브모듈을 등록 목적으로 import 하는 것을 허용하기 위함이다.
# paper_radar.models 는 T8 이 추가한 것 — models.py 는 dataclass 정의만 있는
# 순수 값 타입 모듈(네트워크·DB·다른 계층 의존 없음)이라 sources 가 만드는
# 레코드 타입(OaLocationRecord 등, T3 설계 문서가 "저장 계층이 upsert 시
# 사용할" 것으로 이미 예정해 둔 것)을 소스가 직접 구성해 돌려줘도 계층 방향을
# 어기지 않는다 — T11~T13 의 향후 소스(TrialRecord/IngredientRecord 등)도
# 같은 이유로 이 허용을 그대로 쓴다.
_SOURCES_ALLOWED_PREFIXES = (
    "paper_radar.contract",
    "paper_radar.registry",
    "paper_radar.transport.errors",
    "paper_radar.sources",
    "paper_radar.models",
)

# storage/*: T4 규칙 — storage 는 sqlite3 + stdlib 로 독립적이어야 한다(다른
# 계층에 의존하면 "DB 계층만 떼어 재사용/테스트"가 불가능해진다). 같은 계층
# 내부 조립(schema<-migrations, repository/runlog<-schema)만 허용.
# paper_radar.models 는 T8 이 추가한 것 — repository.upsert_records() 의
# TABLE_FOR 가 dataclass 타입(OaLocationRecord 등)을 키로 쓰려면 그 타입을
# import 해야 한다. models.py 는 storage 를 포함해 어느 계층에도 의존하지
# 않는 순수 값 타입 모듈이라 이 의존이 역방향 계층 위반을 만들지 않는다.
_STORAGE_ALLOWED_PREFIXES = ("paper_radar.storage", "paper_radar.models")

# evidence/*, trend/*, trials/*: 서로의 존재를 몰라야 한다(세 파이프라인은
# 독립 산출물 — SQLite+JSON(papers 테이블) vs CSV vs SQLite(trial 테이블)).
# cli 도 몰라야 한다(cli 는 여러 계층을 조립하는 상위 계층이라 하위 계층이
# 이를 알면 순환 의존이 생긴다). trials(T11)는 원래 이 러너가 cli.py 안에
# 있어 evidence.pipeline/trend.collect 에 이은 세 번째 사본이 CLI 계층에
# 생겼다는 코드리뷰 지적으로 여기로 옮겨졌다 — evidence/trend 와 같은
# 수준의 격리를 이 가드로 강제한다.
_EVIDENCE_FORBIDDEN_PREFIXES = ("paper_radar.trend", "paper_radar.trials", "paper_radar.cli")
_TREND_FORBIDDEN_PREFIXES = ("paper_radar.evidence", "paper_radar.trials", "paper_radar.cli")
_TRIALS_FORBIDDEN_PREFIXES = ("paper_radar.evidence", "paper_radar.trend", "paper_radar.cli")


class LayerDirectionTest(unittest.TestCase):
    """AST 로 src/paper_radar 전 모듈의 import 를 파싱해 계층 규칙을 강제한다.

    위반 시 실패 메시지에 "모듈: import대상" 형태로 전부 나열한다 — 어떤
    파일의 어떤 import 가 규칙을 어겼는지 바로 알 수 있어야 한다."""

    def test_sources_modules_only_import_contract_registry_and_transport_errors(self):
        violations = [
            f"{_module_name(path)}: {target}"
            for path in _iter_py_files()
            if _layer_of(_module_name(path)) == "sources"
            for target in _imports_of(path)
            if not _starts_with_any(target, _SOURCES_ALLOWED_PREFIXES)
        ]
        self.assertEqual(violations, [], f"sources/* 계층 위반: {violations}")

    def test_storage_modules_only_import_other_storage_modules(self):
        violations = [
            f"{_module_name(path)}: {target}"
            for path in _iter_py_files()
            if _layer_of(_module_name(path)) == "storage"
            for target in _imports_of(path)
            if not _starts_with_any(target, _STORAGE_ALLOWED_PREFIXES)
        ]
        self.assertEqual(violations, [], f"storage/* 계층 위반: {violations}")

    def test_evidence_modules_do_not_import_trend_or_cli(self):
        violations = [
            f"{_module_name(path)}: {target}"
            for path in _iter_py_files()
            if _layer_of(_module_name(path)) == "evidence"
            for target in _imports_of(path)
            if _starts_with_any(target, _EVIDENCE_FORBIDDEN_PREFIXES)
        ]
        self.assertEqual(violations, [], f"evidence/* 계층 위반: {violations}")

    def test_trend_modules_do_not_import_evidence_or_cli(self):
        violations = [
            f"{_module_name(path)}: {target}"
            for path in _iter_py_files()
            if _layer_of(_module_name(path)) == "trend"
            for target in _imports_of(path)
            if _starts_with_any(target, _TREND_FORBIDDEN_PREFIXES)
        ]
        self.assertEqual(violations, [], f"trend/* 계층 위반: {violations}")

    def test_trials_modules_do_not_import_evidence_trend_or_cli(self):
        violations = [
            f"{_module_name(path)}: {target}"
            for path in _iter_py_files()
            if _layer_of(_module_name(path)) == "trials"
            for target in _imports_of(path)
            if _starts_with_any(target, _TRIALS_FORBIDDEN_PREFIXES)
        ]
        self.assertEqual(violations, [], f"trials/* 계층 위반: {violations}")

    def test_only_the_entry_point_module_imports_cli(self):
        violations = [
            f"{_module_name(path)}: {target}"
            for path in _iter_py_files()
            if _module_name(path) != "paper_radar.__main__"
            for target in _imports_of(path)
            if _starts_with_any(target, ("paper_radar.cli",))
        ]
        self.assertEqual(violations, [], f"cli import 는 __main__ 만 허용: {violations}")


class SocketGuardTest(unittest.TestCase):
    """tests/conftest.py 의 autouse 소켓 금지 가드 자체를 검증한다 — 가드가
    실제로 소켓 연결을 막는지 확인하지 않으면 가드 코드 자체의 오타나
    monkeypatch 대상 착오를 아무도 잡지 못한다."""

    def test_opening_a_real_socket_connection_is_blocked(self):
        with self.assertRaises(RuntimeError):
            socket.create_connection(("127.0.0.1", 1))

    def test_the_low_level_socket_connect_method_is_also_blocked(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        with self.assertRaises(RuntimeError):
            sock.connect(("127.0.0.1", 1))

    def test_the_blocked_error_message_names_this_test(self):
        with self.assertRaises(RuntimeError) as ctx:
            socket.create_connection(("127.0.0.1", 1))
        self.assertIn(
            "test_the_blocked_error_message_names_this_test", str(ctx.exception)
        )


class SourceDeclarationConsistencyTest(unittest.TestCase):
    """등록된 모든 소스에 대해 모듈.BASE 의 host 와 policy.host 가 일치하는지
    검사한다.

    실제 요청을 보내는 host(BASE 상수의 netloc)와 그 host 에 대한
    페이스/재시도/인증 정책(policy.host)이 어긋나면, 예산·페이스 추적이
    엉뚱한 호스트 앞으로 쌓이고 진짜 요청 대상 호스트는 정책의 보호를 전혀
    받지 못하는 사고가 조용히 생긴다. registry.SOURCES 를 순회하는 식으로
    짜서 새 소스가 @register 되기만 하면 이 검사가 공짜로 적용된다(T10 의
    pubmed 등 미래 소스도 자동 검사 대상이 된다)."""

    def test_every_registered_sources_base_host_matches_its_policy_host(self):
        for key, source_cls in sorted(registry.SOURCES.items()):
            module = importlib.import_module(source_cls.__module__)
            base = getattr(module, "BASE", None)
            self.assertIsNotNone(base, f"{key}: 모듈({module.__name__})에 BASE 상수가 없다")
            netloc = urlsplit(base).netloc
            self.assertEqual(
                netloc,
                source_cls.policy.host,
                f"{key}: BASE netloc({netloc!r}) != policy.host({source_cls.policy.host!r})",
            )

    def test_the_currently_known_sources_are_all_registered(self):
        """레지스트리 순회 검사가 실제로 뭔가를 보고 있는지 확인한다 — 등록
        자체가 (import 순서 문제 등으로) 빠지면 위 검사가 0건을 통과시키며
        조용히 무력화된다. T10 이 pubmed 를 추가해 다섯에서 여섯, T11 이
        clinicaltrials 를 추가해 여섯에서 일곱이 됐다."""
        self.assertEqual(
            set(registry.SOURCES),
            {
                "openalex",
                "crossref",
                "semantic_scholar",
                "europepmc",
                "unpaywall",
                "pubmed",
                "clinicaltrials",
            },
        )


if __name__ == "__main__":
    unittest.main()
