#!/usr/bin/env python3
"""lowprice_bot.py 동작 검증용 오프라인 테스트 스크립트.

외부 네트워크(다나와/네이버 등)에 실제 요청을 보내지 않고,
가상 HTML/JSON 응답 데이터만 주입해 4가지 핵심 요구사항을 검증합니다.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import urllib.error
from typing import Callable, Dict, List, Tuple
from unittest.mock import patch

import lowprice_bot


class FakeHTTPResponse:
    """urllib opener 응답을 대체하는 가상 HTTP 응답 컨텍스트 매니저."""

    def __init__(self, body: str, status: int = 200) -> None:
        self._body = body.encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def make_search_html(items: List[Dict[str, object]], include_ad_carousel: bool = True) -> str:
    """다나와 통합검색 페이지 구조와 동일한 가상 HTML을 생성합니다."""
    parts: List[str] = ["<!DOCTYPE html><html lang='ko'><body>"]
    if include_ad_carousel:
        parts.append(
            '<div class="ad_carousel"><span>이런 상품 어때요?</span><span>광고</span>'
            '<a href="https://prod.danawa.com/info/?pcode=99990001">광고 상품 카루셀 1,000원</a>'
            '<span class="text-px16">1,000원</span></div>'
        )

    parts.append('<ul data-sentry-block="true">')
    for item in items:
        pcode = item["pcode"]
        title = item["title"]
        price = item["price"]
        reviews = item["reviews"]
        is_ad = bool(item.get("is_ad", False))
        ad_markup = '<span class="badge_ad">광고</span>' if is_ad else ""
        parts.append(
            f'<li class="border-b" data-product-seq="{pcode}">'
            f'{ad_markup}'
            f'<img alt="{title}" />'
            f'<a href="https://prod.danawa.com/info/?pcode={pcode}&amp;keyword=test" class="font-bold">{title}</a>'
            f'<span class="sr-only">리뷰수 </span>({reviews})'
            f'<span>20팩</span>'
            f'<span class="text-px16">{price:,}원</span>'
            f'<span>VS상품비교</span>'
            f"</li>"
        )
    parts.append("</ul></body></html>")
    return "".join(parts)


def make_nextjs_detail_html(pcode: str, mall: str, price: int, delivery: str = "무료배송", delivery_fee: int = 0) -> str:
    """다나와 Next.js 상품 상세 페이지의 shopTableRows JSON을 포함한 가상 HTML을 생성합니다."""
    rows = [
        {
            "logo": mall,
            "price": f"{price:,}",
            "delivery": delivery,
            "deliveryPrice": delivery_fee,
            "isCash": False,
            "url": f"https://prod.danawa.com/bridge/loadingBridge.html?pcode={pcode}&cmpnyc=MOCK",
        }
    ]
    payload = json.dumps(rows, ensure_ascii=False).replace('"', '\\"')
    return (
        "<!DOCTYPE html><html><body>"
        f'<script>self.__next_f.push([1,"54:{{\\"lowestPrice\\":\\"{price:,}\\",\\"shopTableRows\\":{payload}}}"])</script>'
        "</body></html>"
    )


def run_bot_with_mock_routes(routes: Dict[str, Callable[[], FakeHTTPResponse]]) -> Tuple[int, str, str]:
    """실제 네트워크 호출 없이 가상 라우터로 lowprice_bot.main()을 실행합니다."""

    def fake_open(req, timeout=25):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for prefix, factory in routes.items():
            if url.startswith(prefix):
                return factory()
        raise AssertionError(f"예상치 못한 외부 URL 요청 시도 차단됨: {url}")

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with patch.object(lowprice_bot._OPENER, "open", side_effect=fake_open):
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            code = lowprice_bot.main()
    return code, stdout_buf.getvalue(), stderr_buf.getvalue()


def test_under_or_equal_20000() -> Tuple[bool, str]:
    """1. 20,000원 이하일 때 특가 메시지가 출력되는가?"""
    search_html = make_search_html(
        [
            {"pcode": "1001", "title": "베베앙 테스트 물티슈 A", "price": 18500, "reviews": 12},
            {"pcode": "1002", "title": "베베앙 테스트 물티슈 B", "price": 22000, "reviews": 5},
            {"pcode": "1003", "title": "베베앙 테스트 물티슈 C", "price": 24000, "reviews": 30},
        ]
    )
    routes = {
        "https://search.danawa.com/dsearch.php": lambda: FakeHTTPResponse(search_html),
        "https://prod.danawa.com/info/?pcode=1001": lambda: FakeHTTPResponse(
            make_nextjs_detail_html("1001", "가상몰A", 18500)
        ),
        "https://prod.danawa.com/info/?pcode=1002": lambda: FakeHTTPResponse(
            make_nextjs_detail_html("1002", "가상몰B", 22000)
        ),
        "https://prod.danawa.com/info/?pcode=1003": lambda: FakeHTTPResponse(
            make_nextjs_detail_html("1003", "가상몰C", 24000)
        ),
    }
    code, out, err = run_bot_with_mock_routes(routes)
    expected = "[특가발견] 목표 가격 이하입니다.!"
    if code != 0:
        return False, f"종료 코드가 0이 아님 (code={code}, err={err.strip()})"
    if expected not in out:
        return False, f"기대 문자열 '{expected}' 미출력 (stdout={out.strip()})"
    if "가상몰A" not in out or "18,500원" not in out:
        return False, f"1위 상품 가격/쇼핑몰 정보 누락 (stdout={out.strip()})"
    return True, f"1위 18,500원 -> '{expected}' 정상 출력 확인"


def test_over_20000() -> Tuple[bool, str]:
    """2. 20,000원 초과일 때 유지 메시지가 출력되는가?"""
    search_html = make_search_html(
        [
            {"pcode": "2001", "title": "베베앙 프리미엄 물티슈 A", "price": 21500, "reviews": 7},
            {"pcode": "2002", "title": "베베앙 프리미엄 물티슈 B", "price": 23000, "reviews": 19},
            {"pcode": "2003", "title": "베베앙 프리미엄 물티슈 C", "price": 25900, "reviews": 41},
        ]
    )
    routes = {
        "https://search.danawa.com/dsearch.php": lambda: FakeHTTPResponse(search_html),
        "https://prod.danawa.com/info/?pcode=2001": lambda: FakeHTTPResponse(
            make_nextjs_detail_html("2001", "가상몰X", 21500)
        ),
        "https://prod.danawa.com/info/?pcode=2002": lambda: FakeHTTPResponse(
            make_nextjs_detail_html("2002", "가상몰Y", 23000)
        ),
        "https://prod.danawa.com/info/?pcode=2003": lambda: FakeHTTPResponse(
            make_nextjs_detail_html("2003", "가상몰Z", 25900)
        ),
    }
    code, out, err = run_bot_with_mock_routes(routes)
    expected = "[유지] 아직 목표 가격보다 비싸요!"
    if code != 0:
        return False, f"종료 코드가 0이 아님 (code={code}, err={err.strip()})"
    if expected not in out:
        return False, f"기대 문자열 '{expected}' 미출력 (stdout={out.strip()})"
    if "21,500원" not in out or "가상몰X" not in out:
        return False, f"1위 상품 가격/쇼핑몰 정보 누락 (stdout={out.strip()})"
    return True, f"1위 21,500원 -> '{expected}' 정상 출력 확인"


def test_ad_filtering() -> Tuple[bool, str]:
    """3. 광고 상품이 섞여 있을 때 걸러낼 수 있는가?"""
    search_html = make_search_html(
        [
            {"pcode": "9001", "title": "베베앙 초특가 광고상품", "price": 9900, "reviews": 999, "is_ad": True},
            {"pcode": "3001", "title": "베베앙 진짜상품 1", "price": 19500, "reviews": 10, "is_ad": False},
            {"pcode": "3002", "title": "베베앙 진짜상품 2", "price": 22500, "reviews": 20, "is_ad": False},
            {"pcode": "3003", "title": "베베앙 진짜상품 3", "price": 24500, "reviews": 30, "is_ad": False},
        ],
        include_ad_carousel=True,
    )
    routes = {
        "https://search.danawa.com/dsearch.php": lambda: FakeHTTPResponse(search_html),
        "https://prod.danawa.com/info/?pcode=3001": lambda: FakeHTTPResponse(
            make_nextjs_detail_html("3001", "정상몰1", 19500)
        ),
        "https://prod.danawa.com/info/?pcode=3002": lambda: FakeHTTPResponse(
            make_nextjs_detail_html("3002", "정상몰2", 22500)
        ),
        "https://prod.danawa.com/info/?pcode=3003": lambda: FakeHTTPResponse(
            make_nextjs_detail_html("3003", "정상몰3", 24500)
        ),
    }
    code, out, err = run_bot_with_mock_routes(routes)
    if code != 0:
        return False, f"종료 코드가 0이 아님 (code={code}, err={err.strip()})"
    if "광고 상품 카루셀" in out or "99990001" in out or "베베앙 초특가 광고상품" in out or "9001" in out:
        return False, f"광고 상품이 필터링되지 않고 출력됨 (stdout={out.strip()})"
    if "베베앙 진짜상품 1" not in out or "베베앙 진짜상품 2" not in out or "베베앙 진짜상품 3" not in out:
        return False, f"진짜 상품 3개가 정상 출력되지 않음 (stdout={out.strip()})"
    return True, "광고 카루셀(99990001) 및 광고 상품(9001) 제외 후 진짜 상품 3개만 출력 확인"


def test_error_handling_without_hang_or_fabrication() -> Tuple[bool, str]:
    """4. 값을 가져오지 못했을 경우 멈추지 않고 오류를 알려주는가?"""
    # 4-A: 네트워크 예외 발생 시
    def raise_network_error():
        raise urllib.error.URLError("mock connection refused")

    code_net, out_net, err_net = run_bot_with_mock_routes(
        {"https://search.danawa.com/dsearch.php": raise_network_error}
    )
    if code_net == 0 or "[오류 - 작업 중단]" not in err_net or out_net.strip():
        return False, (
            f"네트워크 실패 시 오류 출력/중단 처리 미흡 (code={code_net}, out={out_net!r}, err={err_net!r})"
        )

    # 4-B: 비어 있거나 깨진 HTML 응답 시 지어내지 않고 오류 보고
    code_bad, out_bad, err_bad = run_bot_with_mock_routes(
        {"https://search.danawa.com/dsearch.php": lambda: FakeHTTPResponse("<html><body>broken</body></html>")}
    )
    if code_bad == 0 or "[오류 - 작업 중단]" not in err_bad or out_bad.strip():
        return False, (
            f"빈/깨진 HTML 응답 시 오류 출력/중단 처리 미흡 (code={code_bad}, out={out_bad!r}, err={err_bad!r})"
        )

    return True, "네트워크 오류 및 깨진 HTML 응답 모두 지어내지 않고 원인 오류 메시지 출력 후 즉시 종료 확인"


def main() -> int:
    lowprice_bot.configure_utf8_stdio()
    checks = [
        ("1. 20,000원 이하일 때 특가 메시지 출력", test_under_or_equal_20000),
        ("2. 20,000원 초과일 때 유지 메시지 출력", test_over_20000),
        ("3. 광고 상품이 섞여 있을 때 필터링", test_ad_filtering),
        ("4. 값을 가져오지 못했을 경우 멈추지 않고 오류 안내", test_error_handling_without_hang_or_fabrication),
    ]

    print("=" * 72)
    print("lowprice_bot.py 오프라인 가상 데이터 검사 결과")
    print("=" * 72)

    all_passed = True
    for label, fn in checks:
        try:
            ok, detail = fn()
        except Exception as exc:
            ok, detail = False, f"예외 발생: {type(exc).__name__}: {exc}"
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_passed = False
        print(f"- [{status}] {label}")
        print(f"         세부: {detail}")

    print("=" * 72)
    print(f"최종 결과: {'ALL PASS (4/4)' if all_passed else 'FAIL'}")
    print("=" * 72)
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
