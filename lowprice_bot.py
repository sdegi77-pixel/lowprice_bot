#!/usr/bin/env python3
"""다나와 가격비교 공개 표면(danawa-price-search 스킬 방식) 기반 물티슈 최저가 알림 봇.

기능:
1. 다나와 공개 검색/가격비교 표면을 읽기 전용으로 조회하여 광고를 제외한 최저가 상품 Top 3 정리
2. 1위 가격이 목표가(20,000원) 이하일 때만 카카오톡 '나에게 보내기'로 알림 전송
3. 목표가 초과 시 화면에만 출력하고 메시지는 전송하지 않음
4. 알림 전송 실패 시 보낸 척하지 않고 무엇이 왜 잘못되었는지 구체적 에러 출력
"""
from __future__ import annotations

import html
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

QUERY = "베베앙 20개"
TARGET_PRICE = 20000
TOP_N = 3
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/121 Safari/537.36"
)

ENV_PATH = pathlib.Path(__file__).resolve().parent / ".env"


class BotFetchError(RuntimeError):
    """데이터 조회, 파싱 또는 알림 전송 실패 시 발생하는 예외."""


def configure_utf8_stdio() -> None:
    """Windows 콘솔 환경에서 한글 출력이 깨지거나 예외가 나지 않도록 설정합니다."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


class _RedirectWith308Handler(urllib.request.HTTPRedirectHandler):
    """Python 3.10 urllib가 기본 처리하지 않는 HTTP 308 리다이렉트를 처리합니다."""

    def http_error_308(self, req, fp, code, msg, headers):
        return self.http_error_302(req, fp, 307, msg, headers)


_OPENER = urllib.request.build_opener(_RedirectWith308Handler())


def fetch_text(
    url: str,
    *,
    method: str = "GET",
    form_data: Optional[Dict[str, str]] = None,
    referer: Optional[str] = None,
) -> str:
    """다나와 공개 페이지를 HTTP로 조회하고 차단/오류 응답을 검증합니다."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    }
    body: Optional[bytes] = None
    if form_data is not None:
        body = urllib.parse.urlencode(form_data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        headers["X-Requested-With"] = "XMLHttpRequest"
    if referer:
        headers["Referer"] = referer

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=25) as resp:
            status = getattr(resp, "status", 200)
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise BotFetchError(f"HTTP 오류 발생 (status={exc.code}, url={url})") from exc
    except urllib.error.URLError as exc:
        raise BotFetchError(f"네트워크 연결 실패 (reason={exc.reason}, url={url})") from exc
    except TimeoutError as exc:
        raise BotFetchError(f"요청 시간 초과 (url={url})") from exc

    if status != 200 or not raw.strip():
        raise BotFetchError(f"비정상 응답 수신 (status={status}, length={len(raw)}, url={url})")

    lower = raw.lower()
    if any(marker in lower for marker in ("captcha", "g-recaptcha", "turnstile")) or (
        "비정상적인 접근" in raw or "자동화된 접근" in raw
    ):
        raise BotFetchError(f"접근 제한 또는 보안 확인(CAPTCHA) 화면이 반환되었습니다 (url={url})")

    return raw


def clean_text(raw: Optional[str]) -> str:
    """HTML 태그 및 엔티티를 제거하고 공백을 정규화합니다."""
    if not raw:
        return ""
    without_tags = re.sub(r"<[^>]+>", " ", raw)
    return " ".join(html.unescape(without_tags).split())


def parse_int(raw: Optional[str]) -> Optional[int]:
    """문자열에서 숫자만 추출해 정수로 변환합니다."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    return int(digits) if digits else None


def abs_url(url: Optional[str]) -> str:
    """상대 경로를 다나와 절대 HTTPS URL로 변환합니다."""
    if not url:
        return ""
    decoded = html.unescape(url).strip()
    if decoded.startswith("//"):
        return "https:" + decoded
    if decoded.startswith("/"):
        return "https://prod.danawa.com" + decoded
    return decoded


def search_danawa_products(query: str) -> List[Dict[str, Any]]:
    """다나와 통합검색 결과에서 광고를 제외한 실제 가격비교 상품 목록을 추출합니다."""
    search_url = "https://search.danawa.com/dsearch.php?query=" + urllib.parse.quote(query)
    page_html = fetch_text(search_url)

    if "검색결과가 없어" in page_html:
        raise BotFetchError(f"검색어 '{query}'에 대한 정확한 검색 결과가 없습니다.")

    chunks = re.split(r'<li\b[^>]*\bdata-product-seq="', page_html)
    if len(chunks) <= 1:
        raise BotFetchError(
            "다나와 검색 결과에서 상품 목록(data-product-seq)을 찾지 못했습니다. "
            "페이지 구조가 변경되었거나 결과가 비어 있습니다."
        )

    products: List[Dict[str, Any]] = []
    seen_pcodes = set()

    for chunk in chunks[1:]:
        pcode_match = re.match(r'^(\d+)"', chunk)
        if not pcode_match:
            continue
        pcode = pcode_match.group(1)
        if pcode in seen_pcodes:
            continue

        # 광고/스폰서 블록 명시적 제거 (다나와의 광고 캐러셀 및 광고 배지 제외)
        end_idx = chunk.find("VS상품비교")
        item_html = chunk[: end_idx + 120] if end_idx > 0 else chunk[:15000]
        if any(ad_kw in item_html for ad_kw in ('"isAd":true', "adsmart", "이런 상품 어때요?", ">광고<")):
            continue

        title_match = re.search(
            rf'href="https://prod\.danawa\.com/info/\?pcode={re.escape(pcode)}[^"]*"[^>]*class="[^"]*font-bold[^"]*"[^>]*>(.*?)</a>',
            item_html,
            re.S,
        )
        title = clean_text(title_match.group(1)) if title_match else ""
        if not title:
            alt_match = re.search(r'<img[^>]*\balt="([^"]+)"', item_html)
            title = clean_text(alt_match.group(1)) if alt_match else ""

        pack_match = re.search(r">(\d+팩|\d+개)<", item_html)
        if pack_match and pack_match.group(1) not in title:
            title = f"{title} ({pack_match.group(1)})"

        price_match = re.search(r'<span class="text-px16">([0-9,]+)원</span>', item_html)
        price = parse_int(price_match.group(1)) if price_match else None

        review_match = re.search(r"리뷰수\s*</span>\((?:<!--\s*-->)?([\d,]+)(?:<!--\s*-->)?\)", item_html)
        review_count = parse_int(review_match.group(1)) if review_match else 0

        if not title or price is None or price <= 0:
            continue

        seen_pcodes.add(pcode)
        products.append(
            {
                "pcode": pcode,
                "title": title,
                "search_price": price,
                "review_count": review_count,
                "product_url": f"https://prod.danawa.com/info/?pcode={pcode}",
            }
        )

    if not products:
        raise BotFetchError("광고를 제외하고 파싱한 유효 상품이 0건입니다.")

    products.sort(key=lambda x: (x["search_price"], x["pcode"]))
    return products


def extract_js_value(page_html: str, key: str) -> str:
    """레거시 다나와 상품 페이지의 JS 설정 객체에서 키 값을 추출합니다."""
    patterns = [
        rf'{re.escape(key)}\s*:\s*"([^"]*)"',
        rf"{re.escape(key)}\s*:\s*'([^']*)'",
        rf"{re.escape(key)}\s*:\s*([0-9]+)",
    ]
    for pat in patterns:
        m = re.search(pat, page_html)
        if m:
            raw = m.group(1).replace("\\/", "/")
            if "\\u" in raw:
                try:
                    return json.loads('"' + raw.replace('"', '\\"') + '"')
                except Exception:
                    return raw
            return raw
    return ""


def parse_nextjs_offers(page_html: str) -> List[Dict[str, Any]]:
    """신형 Next.js 기반 다나와 상품 페이지의 shopTableRows JSON에서 판매처 목록을 추출합니다."""
    unescaped = page_html.replace('\\"', '"').replace("\\u0026", "&")
    match = re.search(r'"shopTableRows":(\[.*?\])(?:,"[a-zA-Z]|})', unescaped, re.S)
    if not match:
        return []

    try:
        rows_raw = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise BotFetchError(f"상품 상세 JSON(shopTableRows) 파싱 실패: {exc}") from exc

    offers: List[Dict[str, Any]] = []
    for row in rows_raw:
        mall = clean_text(str(row.get("logo") or ""))
        price = parse_int(str(row.get("price") or ""))
        if not mall or price is None or price <= 0:
            continue
        delivery = clean_text(str(row.get("delivery") or "")) or "확인 필요"
        delivery_fee = row.get("deliveryPrice")
        if not isinstance(delivery_fee, int):
            delivery_fee = 0 if "무료" in delivery else (parse_int(delivery) or 0)
        offers.append(
            {
                "mall": mall,
                "price": price,
                "shipping": delivery,
                "shipping_fee": delivery_fee,
                "total_price": price + delivery_fee,
                "is_conditional": bool(row.get("isCash")),
                "url": abs_url(row.get("url")),
            }
        )
    return offers


def parse_legacy_ajax_offers(pcode: str, detail_html: str, detail_url: str) -> List[Dict[str, Any]]:
    """레거시 다나와 상품 페이지의 가격비교 AJAX 응답에서 판매처 목록을 추출합니다."""
    title_match = re.search(r'class="prod_tit"[^>]*>.*?class="title"[^>]*>(.*?)</', detail_html, re.S)
    full_name = clean_text(title_match.group(1)) if title_match else extract_js_value(detail_html, "sProductName")

    payload = {
        "pcode": pcode,
        "cate1": extract_js_value(detail_html, "nCategoryCode1"),
        "cate2": extract_js_value(detail_html, "nCategoryCode2"),
        "cate3": extract_js_value(detail_html, "nCategoryCode3"),
        "cate4": extract_js_value(detail_html, "nCategoryCode4") or "0",
        "UICategoryCode": extract_js_value(detail_html, "nCategoryCode") or "0",
        "powerLinkKeyword": extract_js_value(detail_html, "powerLinkKeyword"),
        "minPrice": extract_js_value(detail_html, "nMinPrice"),
        "keyword": extract_js_value(detail_html, "sKeyword"),
        "NaPm": extract_js_value(detail_html, "sNaPm"),
        "bDeliveryLeftRightYN": "N",
        "bQuickPostSortYN": "N",
        "sSortType": "minPrice",
        "sProductFullName": full_name,
        "bPostPriceYN": "N",
        "bBadgeDefaultYN": "N",
        "bWarrantyDefaultYN": "N",
        "nOpenMarketMoreCount": "30",
        "nAffiliateMoreCount": "30",
        "nOverseasShoppingMoreCount": "30",
        "nGeneralAffiliateMoreCount": "3",
        "sRelationMenuType": "",
        "sRelationType": "",
        "bCoupangSortYN": "N",
        "makerCode": extract_js_value(detail_html, "makerCode"),
        "makerName": extract_js_value(detail_html, "makerName"),
    }
    if not payload["cate1"]:
        return []

    ajax_html = fetch_text(
        "https://prod.danawa.com/info/ajax/getAllPriceCompareMallList.ajax.php",
        method="POST",
        form_data=payload,
        referer=detail_url,
    )

    offers: List[Dict[str, Any]] = []
    for block in ajax_html.split('class="diff_item')[1:]:
        mall_match = re.search(r'<div class="d_mall"[^>]*>.*?alt="([^"]+)"', block, re.S)
        if mall_match:
            mall = clean_text(mall_match.group(1))
        else:
            dm_match = re.search(r'<div class="d_mall"[^>]*>(.*?)</div>', block, re.S)
            mall = clean_text(dm_match.group(1)).removesuffix("신고").strip() if dm_match else ""

        price_match = re.search(r'<em class="prc_[ct]">([^<]+)</em>', block)
        price = parse_int(price_match.group(1)) if price_match else None
        if not mall or price is None or price <= 0:
            continue

        ship_match = re.search(r'class="(?:ship|stxt)[^"]*"[^>]*>(.*?)</(?:span|div)>', block, re.S)
        shipping = clean_text(ship_match.group(1)).strip("()") if ship_match else "확인 필요"
        shipping_fee = 0 if "무료" in shipping else (parse_int(shipping) or 0)

        link_match = re.search(r'href="([^"]+)"[^>]*class="[^"]*priceCompareBuyLink', block)
        if not link_match:
            link_match = re.search(r'class="[^"]*priceCompareBuyLink[^"]*"[^>]*href="([^"]+)"', block)
        buy_url = abs_url(link_match.group(1)) if link_match else ""

        offers.append(
            {
                "mall": mall,
                "price": price,
                "shipping": shipping,
                "shipping_fee": shipping_fee,
                "total_price": price + shipping_fee,
                "is_conditional": "ico cash" in block,
                "url": buy_url,
            }
        )
    return offers


def fetch_lowest_offer(product: Dict[str, Any]) -> Dict[str, Any]:
    """상품 상세 페이지에서 배송비 포함 실구매가 기준 최저가 판매처를 조회합니다."""
    pcode = product["pcode"]
    detail_url = product["product_url"]
    detail_html = fetch_text(detail_url)

    offers = parse_nextjs_offers(detail_html)
    if not offers:
        offers = parse_legacy_ajax_offers(pcode, detail_html, detail_url)

    if not offers:
        raise BotFetchError(
            f"상품(pcode={pcode}, {product['title']})의 쇼핑몰 최저가 목록을 파싱하지 못했습니다."
        )

    offers.sort(key=lambda row: (row["total_price"], row["price"], row["mall"]))
    best = offers[0]
    if not best["mall"] or best["total_price"] <= 0:
        raise BotFetchError(f"상품(pcode={pcode})의 최저가 판매처 값이 올바르지 않습니다: {best}")

    enriched = dict(product)
    enriched.update(
        {
            "price": best["price"],
            "shipping": best["shipping"],
            "total_price": best["total_price"],
            "mall": best["mall"],
            "buy_url": best["url"] or detail_url,
        }
    )
    return enriched


def validate_results(items: List[Dict[str, Any]], expected_count: int) -> None:
    """출력 전 모든 필수 필드가 정상 파싱되었는지 엄격히 검증합니다."""
    if len(items) < expected_count:
        raise BotFetchError(
            f"최소 {expected_count}개 상품이 필요하지만 {len(items)}개만 조회되었습니다."
        )
    for idx, item in enumerate(items[:expected_count], start=1):
        if not item.get("title"):
            raise BotFetchError(f"{idx}위 상품명이 비어 있습니다.")
        if not isinstance(item.get("total_price"), int) or item["total_price"] <= 0:
            raise BotFetchError(f"{idx}위 상품 가격 값이 비정상입니다: {item.get('total_price')}")
        if not item.get("mall"):
            raise BotFetchError(f"{idx}위 쇼핑몰 이름이 비어 있습니다.")
        if not isinstance(item.get("review_count"), int) or item["review_count"] < 0:
            raise BotFetchError(f"{idx}위 리뷰수 값이 비정상입니다: {item.get('review_count')}")
        if not str(item.get("product_url", "")).startswith("https://"):
            raise BotFetchError(f"{idx}위 링크 형식이 올바르지 않습니다: {item.get('product_url')}")


def format_output(items: List[Dict[str, Any]]) -> str:
    """최저가 상위 3개 상품과 목표가 판정 메시지를 보기 좋게 포맷합니다."""
    lines = [
        "=" * 72,
        f"다나와 최저가 검색 결과 | 검색어: \"{QUERY}\" (광고 제외, 실구매가 오름차순 Top {TOP_N})",
        "=" * 72,
    ]
    for rank, item in enumerate(items[:TOP_N], start=1):
        lines.extend(
            [
                f"[{rank}위] {item['title']}",
                f"  - 가격   : {item['total_price']:,}원 (상품가 {item['price']:,}원 / {item['shipping']})",
                f"  - 쇼핑몰 : {item['mall']}",
                f"  - 리뷰수 : {item['review_count']:,}개",
                f"  - 링크   : {item['product_url']}",
                f"  - 구매처 : {item['buy_url']}",
                "-" * 72,
            ]
        )

    top_price = items[0]["total_price"]
    if top_price <= TARGET_PRICE:
        verdict = "[특가발견] 목표 가격 이하입니다.!"
    else:
        verdict = "[유지] 아직 목표 가격보다 비싸요!"
    lines.append(f"목표가({TARGET_PRICE:,}원) 판정 (1위 {top_price:,}원): {verdict}")
    lines.append("=" * 72)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 카카오톡 '나에게 보내기' 알림 기능
# ---------------------------------------------------------------------------

def load_kakao_config(env_path: Optional[pathlib.Path] = None) -> Dict[str, str]:
    """프로젝트 폴더의 .env 파일 또는 시스템 환경 변수에서 카카오 OAuth 설정값을 읽어옵니다."""
    config: Dict[str, str] = {}
    target = env_path or ENV_PATH
    if target.exists():
        for line in target.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            config[k.strip()] = v.strip().strip('"').strip("'")

    # os.environ 환경 변수 지원 (GitHub Actions Secrets 등)
    for env_key in (
        "KAKAO_REST_API_KEY",
        "KAKAO_CLIENT_SECRET",
        "KAKAO_CLIENT_SECREN",
        "KAKAO_REFRESH_TOKEN",
        "KADKO_ REFRESH_TOKEN",
        "KADKO_REFRESH_TOKEN",
    ):
        val = os.environ.get(env_key)
        if val:
            config[env_key] = val.strip()

    client_id = config.get("KAKAO_REST_API_KEY", "")
    client_secret = config.get("KAKAO_CLIENT_SECRET") or config.get("KAKAO_CLIENT_SECREN", "")
    refresh_token = (
        config.get("KAKAO_REFRESH_TOKEN")
        or config.get("KADKO_ REFRESH_TOKEN")
        or config.get("KADKO_REFRESH_TOKEN", "")
    )

    missing = []
    if not client_id:
        missing.append("KAKAO_REST_API_KEY")
    if not refresh_token:
        missing.append("KAKAO_REFRESH_TOKEN (또는 KADKO_ REFRESH_TOKEN)")

    if missing:
        source_desc = f".env 파일({target})" if target.exists() else "환경 변수 및 .env 파일"
        raise BotFetchError(f"{source_desc}에 필수 카카오 인증 정보가 누락되었습니다: {', '.join(missing)}")

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }


def update_env_refresh_token(new_refresh_token: str, env_path: Optional[pathlib.Path] = None) -> None:
    """카카오 서버에서 신규 refresh_token이 발급된 경우 .env 파일에 자동 갱신합니다."""
    target = env_path or ENV_PATH
    if not target.exists():
        return
    lines = target.read_text(encoding="utf-8").splitlines()
    for key in ("KAKAO_REFRESH_TOKEN", "KADKO_ REFRESH_TOKEN"):
        found = False
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={new_refresh_token}"
                found = True
                break
        if not found:
            lines.append(f"{key}={new_refresh_token}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def refresh_kakao_access_token(config: Dict[str, str]) -> str:
    """리프레시 토큰을 이용해 카카오 API 호출용 신규 액세스 토큰을 발급받습니다."""
    url = "https://kauth.kakao.com/oauth/token"
    data = {
        "grant_type": "refresh_token",
        "client_id": config["client_id"],
        "refresh_token": config["refresh_token"],
    }
    if config.get("client_secret"):
        data["client_secret"] = config["client_secret"]

    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
        method="POST",
    )
    try:
        with _OPENER.open(req, timeout=15) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", "replace")
        raise BotFetchError(f"카카오 토큰 갱신 실패 (HTTP {exc.code}): {err_body}") from exc
    except Exception as exc:
        raise BotFetchError(f"카카오 토큰 갱신 중 네트워크 오류 발생: {exc}") from exc

    access_token = res_data.get("access_token")
    if not access_token:
        raise BotFetchError(f"카카오 토큰 갱신 응답에 access_token이 누락되었습니다: {res_data}")

    if res_data.get("refresh_token"):
        update_env_refresh_token(res_data["refresh_token"])

    return access_token


def send_kakao_memo_alert(item: Dict[str, Any]) -> None:
    """1위 특가 상품 정보를 카카오톡 '나에게 보내기' 기본 텍스트 템플릿으로 전송합니다."""
    config = load_kakao_config()
    access_token = refresh_kakao_access_token(config)

    memo_url = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
    title = item.get("title", "상품명 미확인")
    total_price = item.get("total_price", 0)
    price = item.get("price", 0)
    shipping = item.get("shipping", "확인 필요")
    mall = item.get("mall", "쇼핑몰 미확인")
    review_count = item.get("review_count", 0)
    buy_url = item.get("buy_url") or item.get("product_url") or "https://www.danawa.com"

    text = (
        f"[특가발견] 베베앙 20개 최저가 알림!\n\n"
        f"• 상품명: {title}\n"
        f"• 가격: {total_price:,}원 (상품가 {price:,}원 / {shipping})\n"
        f"• 쇼핑몰: {mall}\n"
        f"• 리뷰수: {review_count:,}개"
    )

    template = {
        "object_type": "text",
        "text": text,
        "link": {
            "web_url": buy_url,
            "mobile_web_url": buy_url,
        },
        "button_title": "구매하러 가기",
    }

    form_data = {
        "template_object": json.dumps(template, ensure_ascii=False)
    }
    body = urllib.parse.urlencode(form_data).encode("utf-8")
    req = urllib.request.Request(
        memo_url,
        data=body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        },
        method="POST",
    )

    try:
        with _OPENER.open(req, timeout=15) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", "replace")
        raise BotFetchError(f"카카오톡 메시지 전송 API 호출 실패 (HTTP {exc.code}): {err_body}") from exc
    except Exception as exc:
        raise BotFetchError(f"카카오톡 메시지 전송 중 네트워크 오류 발생: {exc}") from exc

    if res_data.get("result_code") != 0:
        raise BotFetchError(f"카카오톡 메시지 전송 비정상 응답: {res_data}")

    print("[카카오톡 알림 전송 완료] 1위 특가 상품 정보를 카카오톡 '나에게 보내기'로 전송했습니다.")


def main() -> int:
    configure_utf8_stdio()
    try:
        candidates = search_danawa_products(QUERY)
        enriched: List[Dict[str, Any]] = []
        for candidate in candidates[:6]:
            enriched.append(fetch_lowest_offer(candidate))
        enriched.sort(key=lambda x: (x["total_price"], x["price"], x["pcode"]))

        validate_results(enriched, TOP_N)
        print(format_output(enriched))

        # 1위 가격이 목표가(20,000원) 이하일 때만 카카오톡 알림 전송
        top_item = enriched[0]
        if top_item["total_price"] <= TARGET_PRICE:
            try:
                send_kakao_memo_alert(top_item)
            except Exception as exc:
                # 알림을 못 보냈으면 보낸 척하지 않고 무엇이 잘못되었는지 그대로 출력
                print(f"[카카오톡 알림 전송 실패] {exc}", file=sys.stderr)
        else:
            print("[카카오톡 알림 미전송] 1위 가격이 목표가(20,000원)를 초과하여 메시지를 전송하지 않았습니다.")

        return 0
    except BotFetchError as exc:
        print(f"[오류 - 작업 중단] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[예상치 못한 오류 - 작업 중단] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
