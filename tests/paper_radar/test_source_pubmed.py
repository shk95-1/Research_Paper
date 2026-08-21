"""paper_radar.sources.pubmed: 두 단계 조회(esearch -> efetch)와 stdlib XML 파싱.

PubMed 는 다른 보강 소스와 달리 DOI 하나로 바로 레코드를 주지 않는다 —
esearch 로 PMID 를 찾고, 그 PMID 로 efetch 를 다시 불러야 제목/초록/MeSH 를
받는다. 그래서 fetch() 테스트는 항상 두 응답(esearch JSON, efetch XML)을
FakeSession 큐에 순서대로 넣는다.

아래 XML 픽스처는 구성 예시다 — 실제 응답 형태와 100% 같다고 보장하지
않는다. 실검증은 tool/live_smoke.py 가 한다.
"""

import json
import os
import unittest
from unittest import mock

from paper_radar.registry import SOURCES
from paper_radar.sources import pubmed
from paper_radar.transport.errors import NotFound
from paper_radar.transport.http import Transport
from tests.paper_radar.test_transport import FakeResponse, FakeSession, make_clock_and_sleep

ESEARCH_FOUND = {"esearchresult": {"idlist": ["28559157"], "count": "1"}}
ESEARCH_EMPTY = {"esearchresult": {"idlist": [], "count": "0"}}

EFETCH_XML = """<?xml version="1.0" ?>
<PubmedArticleSet>
<PubmedArticle>
<MedlineCitation>
<PMID>28559157</PMID>
<Article>
<ArticleTitle>Integrating habits and practices data for soaps and cosmetics</ArticleTitle>
<Abstract>
<AbstractText>Aggregate exposure to fragrance ingredients was modelled.</AbstractText>
</Abstract>
<Journal><Title>Regulatory toxicology and pharmacology : RTP</Title></Journal>
</Article>
<MeshHeadingList>
<MeshHeading><DescriptorName>Cosmetics</DescriptorName></MeshHeading>
<MeshHeading><DescriptorName>Fragrance</DescriptorName></MeshHeading>
</MeshHeadingList>
</MedlineCitation>
<PubmedData>
<ArticleIdList>
<ArticleId IdType="pubmed">28559157</ArticleId>
<ArticleId IdType="doi">10.1016/j.yrtph.2017.05.017</ArticleId>
</ArticleIdList>
</PubmedData>
</PubmedArticle>
</PubmedArticleSet>"""

EFETCH_XML_SECTIONED_ABSTRACT = """<?xml version="1.0" ?>
<PubmedArticleSet>
<PubmedArticle>
<MedlineCitation>
<PMID>1</PMID>
<Article>
<ArticleTitle>Retinol review</ArticleTitle>
<Abstract>
<AbstractText Label="BACKGROUND">Retinol is widely used.</AbstractText>
<AbstractText Label="METHODS">We reviewed the literature.</AbstractText>
</Abstract>
</Article>
</MedlineCitation>
</PubmedArticle>
</PubmedArticleSet>"""

EFETCH_XML_NO_MESH = """<?xml version="1.0" ?>
<PubmedArticleSet>
<PubmedArticle>
<MedlineCitation>
<PMID>2</PMID>
<Article>
<ArticleTitle>A paper without MeSH terms yet</ArticleTitle>
</Article>
</MedlineCitation>
</PubmedArticle>
</PubmedArticleSet>"""

EFETCH_XML_NO_DOI_BACKREFERENCE = """<?xml version="1.0" ?>
<PubmedArticleSet>
<PubmedArticle>
<MedlineCitation>
<PMID>3</PMID>
<Article>
<ArticleTitle>A paper without a DOI in ArticleIdList</ArticleTitle>
</Article>
</MedlineCitation>
<PubmedData>
<ArticleIdList>
<ArticleId IdType="pubmed">3</ArticleId>
</ArticleIdList>
</PubmedData>
</PubmedArticle>
</PubmedArticleSet>"""

# 리뷰 대응 — 중첩 마크업(<i>, <sup>, <sub> 등)이 섞인 실제 PubMed 응답 형태.
# 화장품/피부과 코퍼스에서 <i>학명·성분명</i>(예: Retinol 을 이탤릭으로),
# <sup>/<sub> 동위원소·화학식 표기(예: "13C", "CO2")는 흔하다. ArticleTitle
# 과 섹션 초록 양쪽에 넣어 두 경로 모두(_text() 를 공유하는 title/journal/
# MeSH 와, _abstract_text() 가 섹션마다 _text() 를 호출하는 abstract) 태그
# 안팎의 텍스트를 잃지 않는지 확인한다.
EFETCH_XML_NESTED_MARKUP = """<?xml version="1.0" ?>
<PubmedArticleSet>
<PubmedArticle>
<MedlineCitation>
<PMID>4</PMID>
<Article>
<ArticleTitle>Effects of <i>Retinol</i> on skin: a <sup>13</sup>C study</ArticleTitle>
<Abstract>
<AbstractText Label="BACKGROUND">Effects of <i>Retinol</i> on skin.</AbstractText>
<AbstractText Label="METHODS">We measured CO<sub>2</sub> output.</AbstractText>
</Abstract>
</Article>
</MedlineCitation>
</PubmedArticle>
</PubmedArticleSet>"""

BROKEN_XML = "<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>1</PMID>"

RECORD_KEYS = {"title", "abstract", "journal", "mesh_terms", "pmid"}


class ParseEfetchXmlTest(unittest.TestCase):
    def test_parses_the_normal_shape(self):
        result = pubmed.parse_efetch_xml(EFETCH_XML)
        self.assertEqual(result["pmid"], "28559157")
        self.assertEqual(
            result["title"], "Integrating habits and practices data for soaps and cosmetics"
        )
        self.assertEqual(
            result["abstract"], "Aggregate exposure to fragrance ingredients was modelled."
        )
        self.assertEqual(result["journal"], "Regulatory toxicology and pharmacology : RTP")
        self.assertEqual(result["mesh_terms"], ("Cosmetics", "Fragrance"))
        self.assertEqual(result["doi"], "10.1016/j.yrtph.2017.05.017")

    def test_joins_a_sectioned_abstract_with_a_single_space(self):
        # Label 이 있는 여러 AbstractText 는 순서대로 공백 하나로 이어붙인다.
        result = pubmed.parse_efetch_xml(EFETCH_XML_SECTIONED_ABSTRACT)
        self.assertEqual(
            result["abstract"], "Retinol is widely used. We reviewed the literature."
        )

    def test_reports_an_empty_tuple_when_there_is_no_mesh_heading_list(self):
        result = pubmed.parse_efetch_xml(EFETCH_XML_NO_MESH)
        self.assertEqual(result["mesh_terms"], ())

    def test_reports_no_doi_backreference_when_the_article_id_list_lacks_one(self):
        result = pubmed.parse_efetch_xml(EFETCH_XML_NO_DOI_BACKREFERENCE)
        self.assertIsNone(result["doi"])

    def test_raises_value_error_instead_of_the_underlying_xml_parse_error(self):
        """XML 파싱은 transport 밖에서 일어나므로 transport.errors.ParseError
        가 아니라 명시적 ValueError 여야 한다(모듈 docstring 참고)."""
        with self.assertRaises(ValueError):
            pubmed.parse_efetch_xml(BROKEN_XML)

    def test_recovers_the_full_title_text_around_nested_markup(self):
        # <i>/<sup> 안팎의 텍스트가 사라지지 않고 이어붙어야 한다(리뷰 대응).
        result = pubmed.parse_efetch_xml(EFETCH_XML_NESTED_MARKUP)
        self.assertEqual(result["title"], "Effects of Retinol on skin: a 13C study")

    def test_recovers_the_full_abstract_text_around_nested_markup_across_sections(self):
        # 섹션 초록 + 마크업 조합: 두 AbstractText 각각의 <i>/<sub> 안팎
        # 텍스트가 보존된 채로, 섹션 사이는 여전히 공백 하나로 이어붙는다.
        result = pubmed.parse_efetch_xml(EFETCH_XML_NESTED_MARKUP)
        self.assertEqual(
            result["abstract"], "Effects of Retinol on skin. We measured CO2 output."
        )


class FetchTest(unittest.TestCase):
    def _transport(self, responses):
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession(responses)
        return Transport(session=session, clock=clock, sleep=sleep), session

    def test_returns_none_without_a_doi(self):
        transport, session = self._transport([])
        self.assertIsNone(pubmed.fetch(transport, doi=None, title="Some title"))
        self.assertEqual(session.calls, [])

    def test_returns_none_when_esearch_finds_no_pmids(self):
        transport, session = self._transport(
            [FakeResponse(200, body=json.dumps(ESEARCH_EMPTY).encode())]
        )
        result = pubmed.fetch(transport, doi="10.9999/unknown")
        self.assertIsNone(result)
        self.assertEqual(len(session.calls), 1)  # efetch 는 호출되지 않는다

    def test_esearch_is_called_with_the_expected_parameters(self):
        transport, session = self._transport(
            [
                FakeResponse(200, body=json.dumps(ESEARCH_FOUND).encode()),
                FakeResponse(200, body=EFETCH_XML.encode()),
            ]
        )
        pubmed.fetch(transport, doi="10.1016/j.yrtph.2017.05.017")
        params = session.calls[0]["params"]
        self.assertEqual(params["db"], "pubmed")
        self.assertEqual(params["term"], "10.1016/j.yrtph.2017.05.017[DOI]")
        self.assertEqual(params["retmode"], "json")

    def test_efetch_is_called_with_the_pmid_and_xml_retmode(self):
        transport, session = self._transport(
            [
                FakeResponse(200, body=json.dumps(ESEARCH_FOUND).encode()),
                FakeResponse(200, body=EFETCH_XML.encode()),
            ]
        )
        pubmed.fetch(transport, doi="10.1016/j.yrtph.2017.05.017")
        params = session.calls[1]["params"]
        self.assertEqual(params["db"], "pubmed")
        self.assertEqual(params["id"], "28559157")
        self.assertEqual(params["retmode"], "xml")

    def test_returns_the_five_key_record_shape(self):
        transport, _ = self._transport(
            [
                FakeResponse(200, body=json.dumps(ESEARCH_FOUND).encode()),
                FakeResponse(200, body=EFETCH_XML.encode()),
            ]
        )
        result = pubmed.fetch(transport, doi="10.1016/j.yrtph.2017.05.017")
        self.assertEqual(set(result), RECORD_KEYS)
        self.assertEqual(result["mesh_terms"], ("Cosmetics", "Fragrance"))
        self.assertEqual(result["pmid"], "28559157")

    def test_propagates_not_found_instead_of_absorbing_it(self):
        """구 papers/http.py 는 404 를 None 으로 흡수했지만, 새 계약에서는
        transport 의 NotFound 를 여기서 잡지 않고 그대로 전파해야 한다."""
        transport, session = self._transport([FakeResponse(404)])
        with self.assertRaises(NotFound):
            pubmed.fetch(transport, doi="10.9999/absent")
        self.assertEqual(len(session.calls), 1)

    def test_propagates_a_value_error_from_a_broken_efetch_response(self):
        transport, _ = self._transport(
            [
                FakeResponse(200, body=json.dumps(ESEARCH_FOUND).encode()),
                FakeResponse(200, body=BROKEN_XML.encode()),
            ]
        )
        with self.assertRaises(ValueError):
            pubmed.fetch(transport, doi="10.1016/j.yrtph.2017.05.017")


class SearchPmidsTest(unittest.TestCase):
    def _transport(self, responses):
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession(responses)
        return Transport(session=session, clock=clock, sleep=sleep), session

    def test_returns_the_id_list(self):
        transport, _ = self._transport(
            [FakeResponse(200, body=json.dumps(ESEARCH_FOUND).encode())]
        )
        self.assertEqual(pubmed.search_pmids(transport, "cosmetic"), ["28559157"])

    def test_returns_an_empty_list_for_no_hits(self):
        transport, _ = self._transport(
            [FakeResponse(200, body=json.dumps(ESEARCH_EMPTY).encode())]
        )
        self.assertEqual(pubmed.search_pmids(transport, "nonsense query"), [])


class CurrentPolicyTest(unittest.TestCase):
    def test_uses_the_anonymous_interval_without_an_api_key(self):
        with mock.patch.dict(os.environ, {"NCBI_API_KEY": ""}, clear=False):
            self.assertEqual(pubmed.current_policy().min_interval_s, 0.34)

    def test_uses_the_keyed_interval_when_an_api_key_is_set(self):
        with mock.patch.dict(os.environ, {"NCBI_API_KEY": "secret"}, clear=False):
            self.assertEqual(pubmed.current_policy().min_interval_s, 0.1)

    def test_keeps_the_auth_declaration_in_both_cases(self):
        with mock.patch.dict(os.environ, {"NCBI_API_KEY": "secret"}, clear=False):
            policy = pubmed.current_policy()
        self.assertEqual(policy.auth_kind, "param")
        self.assertEqual(policy.auth_name, "api_key")
        self.assertEqual(policy.auth_env, "NCBI_API_KEY")


class RegistryTest(unittest.TestCase):
    def test_registers_pubmed_under_its_key(self):
        self.assertIs(SOURCES["pubmed"], pubmed.PubMed)


if __name__ == "__main__":
    unittest.main()
