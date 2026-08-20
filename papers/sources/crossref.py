"""Crossref — DOI 검증 담당.

이 소스의 역할은 초록 보강이 아니다. '이 DOI 가 실제로 등록된 논문인가'와
'등록된 제목이 OpenAlex 제목과 같은가'에 답하는 것이다. 그 두 답이
confidence_score 의 55점(30 + 25)을 결정한다.

없는 DOI 는 404 를 준다. http.get_json 이 이를 None 으로 흡수하므로
여기서는 None 을 그대로 흘려보내면 crossref_verified = false 가 된다.

응답의 title 과 container-title 은 문자열이 아니라 리스트다.
"""

from urllib.parse import quote

from .. import http

BASE = "https://api.crossref.org/works/"


def _first(value):
    """Crossref 의 리스트 필드에서 첫 값을 꺼낸다."""
    if isinstance(value, list):
        return value[0] if value else None
    return value or None


def _year(issued):
    parts = (issued or {}).get("date-parts") or []
    if not parts or not isinstance(parts[0], list) or not parts[0]:
        return None
    year = parts[0][0]
    return year if isinstance(year, int) else None


def _polite():
    params = {}
    email = http.contact_email()
    if email:
        params["mailto"] = email
    return params


def fetch(doi=None, title=None):
    """DOI 로만 조회한다. 미발견이나 실패는 None."""
    if not doi:
        return None
    payload = http.get_json(BASE + quote(doi, safe="/"), params=_polite())
    if not payload:
        return http.TRANSIENT if payload is http.TRANSIENT else None
    message = payload.get("message")
    if not isinstance(message, dict):
        return None
    return {
        "title": _first(message.get("title")),
        "journal": _first(message.get("container-title")),
        "publisher": message.get("publisher") or None,
        "type": message.get("type") or None,
        "year": _year(message.get("issued")),
    }
