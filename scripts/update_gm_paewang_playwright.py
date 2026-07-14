#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.sync_api import BrowserContext, Frame, Page, TimeoutError, sync_playwright

CAFE_ID = "31717562"
MENU_ID = "48"
BOARD_URL = "https://cafe.naver.com/f-e/cafes/31717562/menus/48?viewType=L"
OUTPUT = Path(os.environ.get("GM_OUTPUT", "gm-paewang-posts.json"))
MAX_LIST_POSTS = int(os.environ.get("GM_MAX_POSTS", "40"))
MAX_NEW_DETAILS = int(os.environ.get("GM_MAX_NEW_DETAILS", "20"))
SUMMARY_LIMIT = 420
KST = timezone(timedelta(hours=9))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

ARTICLE_LINK_SELECTORS = [
    'a[href*="ArticleRead.nhn"]',
    'a[href*="/articles/"]',
    'a[href*="articleid="]',
    'a[href*="articleId="]',
]

TITLE_SELECTORS = [
    "h1",
    "h2",
    ".ArticleTitle .title_text",
    ".ArticleTitle",
    ".title_text",
    '[class*="ArticleTitle"]',
    '[class*="article_title"]',
    'meta[property="og:title"]',
]

AUTHOR_SELECTORS = [
    ".nickname",
    ".WriterInfo .nickname",
    ".ArticleWriter",
    '[class*="writer"]',
    '[class*="nickname"]',
]

DATE_SELECTORS = [
    "time",
    ".date",
    ".ArticleTool .date",
    '[class*="write_date"]',
    '[class*="article_date"]',
    '[class*="date"]',
]

BODY_SELECTORS = [
    ".se-main-container",
    ".article_viewer",
    ".ContentRenderer",
    "#tbody",
    ".ArticleContentBox",
    '[class*="article_body"]',
    '[class*="ArticleContent"]',
    '[class*="content_viewer"]',
    "article",
]

NOISE_LINES = {
    "공유",
    "댓글",
    "좋아요",
    "신고",
    "목록",
    "이전글",
    "다음글",
    "작성자",
    "조회",
    "본문 기타 기능",
}


def now_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def text_clean(value: str) -> str:
    value = value.replace("\u200b", " ").replace("\xa0", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n[ \t]+", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def compact_lines(value: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    for raw in value.splitlines():
        line = text_clean(raw)
        if not line:
            continue
        if line in NOISE_LINES:
            continue
        if len(line) <= 1:
            continue
        if re.fullmatch(r"[\d\s·:./-]+", line):
            continue
        if line in seen:
            continue

        seen.add(line)
        result.append(line)

    return result


def make_summary(body_text: str, title: str = "") -> str:
    lines = compact_lines(body_text)
    title_key = text_clean(title)

    filtered: list[str] = []
    for line in lines:
        if title_key and line == title_key:
            continue
        if line.startswith("https://") or line.startswith("http://"):
            continue
        filtered.append(line)

    joined = "\n".join(filtered)
    if not joined:
        return "본문 핵심 내용을 확인하지 못했습니다. 원문 게시글에서 확인해 주세요."

    sentences = re.split(r"(?<=[.!?。！？])\s+|\n+", joined)
    selected: list[str] = []
    length = 0

    for sentence in sentences:
        sentence = text_clean(sentence)
        if len(sentence) < 4:
            continue
        if sentence in NOISE_LINES:
            continue

        remaining = SUMMARY_LIMIT - length
        if remaining <= 0:
            break

        if len(sentence) > remaining:
            sentence = sentence[: max(0, remaining - 1)].rstrip() + "…"

        selected.append(sentence)
        length += len(sentence) + 1

        if len(selected) >= 4 or length >= SUMMARY_LIMIT:
            break

    summary = "\n".join(selected).strip()
    return summary or joined[:SUMMARY_LIMIT].rstrip() + ("…" if len(joined) > SUMMARY_LIMIT else "")


def article_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    for key in ("articleid", "articleId", "article_id"):
        if key in query and query[key]:
            return str(query[key][0])

    patterns = [
        r"/articles/(\d+)",
        r"/article/(\d+)",
        r"articleid=(\d+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, url, flags=re.I)
        if match:
            return match.group(1)

    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def resolve_browser_path() -> str | None:
    env_path = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    candidates = [
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
    ]

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)

    return None


def load_existing() -> dict[str, Any]:
    if not OUTPUT.exists():
        return {"meta": {}, "items": []}

    try:
        payload = json.loads(OUTPUT.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass

    return {"meta": {}, "items": []}


def all_frames(page: Page) -> list[Frame]:
    return list(page.frames)


def close_popups(page: Page) -> None:
    labels = ["닫기", "취소", "나중에", "확인"]

    for frame in all_frames(page):
        for label in labels:
            try:
                button = frame.get_by_role("button", name=label, exact=True)
                if button.count():
                    button.first.click(timeout=500)
            except Exception:
                continue


def extract_locator_text(frame: Frame, selectors: list[str]) -> str:
    for selector in selectors:
        try:
            locator = frame.locator(selector)
            count = min(locator.count(), 8)

            for index in range(count):
                item = locator.nth(index)

                if selector.startswith("meta"):
                    value = item.get_attribute("content")
                else:
                    value = item.inner_text(timeout=1500)

                value = text_clean(value or "")
                if value:
                    return value
        except Exception:
            continue

    return ""


def collect_article_links(page: Page) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}

    for frame in all_frames(page):
        for selector in ARTICLE_LINK_SELECTORS:
            try:
                links = frame.locator(selector)
                count = min(links.count(), 250)

                for index in range(count):
                    anchor = links.nth(index)
                    href = anchor.get_attribute("href") or ""
                    title = text_clean(anchor.inner_text(timeout=1000) or "")

                    if not href:
                        continue

                    absolute = urljoin(frame.url or BOARD_URL, href)
                    lowered = absolute.lower()

                    if "cafe.naver.com" not in lowered:
                        continue
                    if not (
                        "articleread.nhn" in lowered
                        or "/articles/" in lowered
                        or "articleid=" in lowered
                    ):
                        continue

                    parsed = urlparse(absolute)
                    query = parse_qs(parsed.query)
                    menu_values = query.get("menuid") or query.get("menuId")
                    if menu_values and MENU_ID not in {str(value) for value in menu_values}:
                        continue

                    article_id = article_id_from_url(absolute)
                    if not article_id:
                        continue

                    if article_id not in found:
                        found[article_id] = {
                            "id": article_id,
                            "articleId": article_id,
                            "title": title or "제목 확인 중",
                            "link": absolute,
                        }
            except Exception:
                continue

    rows = list(found.values())

    def sort_key(item: dict[str, Any]) -> tuple[int, str]:
        article_id = str(item.get("articleId", ""))
        return (int(article_id) if article_id.isdigit() else 0, article_id)

    rows.sort(key=sort_key, reverse=True)
    return rows[:MAX_LIST_POSTS]


def goto_with_fallback(page: Page, url: str) -> None:
    response = page.goto(url, wait_until="domcontentloaded", timeout=40000)
    if response and response.status >= 400:
        raise RuntimeError(f"HTTP {response.status}: {url}")

    page.wait_for_timeout(3500)
    close_popups(page)


def scrape_article_detail(
    context: BrowserContext,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    page = context.new_page()

    try:
        goto_with_fallback(page, str(candidate["link"]))

        title = ""
        writer = ""
        published = ""
        body = ""

        for frame in all_frames(page):
            if not title:
                title = extract_locator_text(frame, TITLE_SELECTORS)
            if not writer:
                writer = extract_locator_text(frame, AUTHOR_SELECTORS)
            if not published:
                published = extract_locator_text(frame, DATE_SELECTORS)

            if not body:
                for selector in BODY_SELECTORS:
                    try:
                        locator = frame.locator(selector)
                        count = min(locator.count(), 5)

                        for index in range(count):
                            text = text_clean(locator.nth(index).inner_text(timeout=2500) or "")
                            if len(text) > len(body):
                                body = text
                    except Exception:
                        continue

        if not title:
            try:
                title = text_clean(page.title())
            except Exception:
                title = ""

        if not body:
            for frame in all_frames(page):
                try:
                    text = text_clean(frame.locator("body").inner_text(timeout=3000) or "")
                    if len(text) > len(body):
                        body = text
                except Exception:
                    continue

        final_title = title or str(candidate.get("title") or "제목 없음")
        summary = make_summary(body, final_title)

        return {
            **candidate,
            "title": final_title,
            "writer": writer or "GM패왕",
            "publishedAt": published,
            "timestamp": published,
            "summary": summary,
            "bodyHash": hashlib.sha256(body.encode("utf-8")).hexdigest() if body else "",
            "collectedAt": now_iso(),
            "collectStatus": "ok",
        }
    except Exception as exc:
        return {
            **candidate,
            "writer": candidate.get("writer") or "GM패왕",
            "summary": "본문 자동 수집에 실패했습니다. 원문 게시글에서 확인해 주세요.",
            "collectedAt": now_iso(),
            "collectStatus": "failed",
            "collectError": str(exc)[:300],
        }
    finally:
        page.close()


def mark_new_posts(items: list[dict[str, Any]], new_ids: set[str]) -> None:
    for item in items:
        item["isNew"] = str(item.get("id")) in new_ids


def main() -> int:
    existing_payload = load_existing()
    existing_items = existing_payload.get("items") or []
    existing_by_id = {
        str(item.get("id") or item.get("articleId")): item
        for item in existing_items
        if isinstance(item, dict)
    }

    executable = resolve_browser_path()

    with sync_playwright() as playwright:
        launch_options: dict[str, Any] = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--lang=ko-KR",
            ],
        }

        if executable:
            launch_options["executable_path"] = executable

        browser = playwright.chromium.launch(**launch_options)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1100},
            user_agent=USER_AGENT,
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            extra_http_headers={
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.5",
            },
        )

        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        list_page = context.new_page()

        try:
            goto_with_fallback(list_page, BOARD_URL)
            candidates = collect_article_links(list_page)
        finally:
            list_page.close()

        if not candidates:
            browser.close()
            raise RuntimeError(
                "GM패왕 게시판에서 게시글 링크를 찾지 못했습니다. "
                "네이버 화면 구조 또는 접근 정책을 확인해야 합니다."
            )

        new_candidates = [
            candidate
            for candidate in candidates
            if str(candidate["id"]) not in existing_by_id
            or not existing_by_id[str(candidate["id"])].get("summary")
        ][:MAX_NEW_DETAILS]

        collected_new: dict[str, dict[str, Any]] = {}

        for index, candidate in enumerate(new_candidates, start=1):
            print(
                f"[{index}/{len(new_candidates)}] "
                f"게시글 열기: {candidate.get('articleId')} {candidate.get('title')}"
            )
            detail = scrape_article_detail(context, candidate)
            detail["firstCollectedAt"] = now_iso()
            collected_new[str(detail["id"])] = detail
            time.sleep(0.7)

        browser.close()

    new_ids = set(collected_new)
    merged: list[dict[str, Any]] = []

    for candidate in candidates:
        article_id = str(candidate["id"])

        if article_id in collected_new:
            merged.append(collected_new[article_id])
            continue

        previous = existing_by_id.get(article_id)
        if previous:
            updated = dict(previous)
            updated["link"] = candidate.get("link") or updated.get("link")
            if candidate.get("title") and candidate.get("title") != "제목 확인 중":
                updated["title"] = candidate["title"]
            merged.append(updated)
            continue

        merged.append(
            {
                **candidate,
                "writer": "GM패왕",
                "summary": "다음 자동 갱신에서 본문 핵심 내용을 수집합니다.",
                "collectedAt": now_iso(),
                "firstCollectedAt": now_iso(),
                "collectStatus": "queued",
            }
        )

    # 목록에서 사라진 기존 글도 최대 100개 범위 안에서 보존
    candidate_ids = {str(item["id"]) for item in candidates}
    for previous in existing_items:
        previous_id = str(previous.get("id") or previous.get("articleId") or "")
        if previous_id and previous_id not in candidate_ids:
            merged.append(previous)

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []

    for item in merged:
        item_id = str(item.get("id") or item.get("articleId") or "")
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        unique.append(item)

    def ordering(item: dict[str, Any]) -> tuple[int, str]:
        article_id = str(item.get("articleId") or item.get("id") or "")
        return (int(article_id) if article_id.isdigit() else 0, article_id)

    unique.sort(key=ordering, reverse=True)
    unique = unique[:100]
    mark_new_posts(unique, new_ids)

    payload = {
        "meta": {
            "boardName": "GM패왕",
            "cafeId": CAFE_ID,
            "menuId": MENU_ID,
            "sourceUrl": BOARD_URL,
            "updatedAt": now_iso(),
            "status": "ok",
            "count": len(unique),
            "newCount": len(new_ids),
            "openedArticleCount": len(new_candidates),
            "collectionMethod": "Playwright browser - open new posts one by one",
        },
        "items": unique,
    }

    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        f"완료: 전체 {len(unique)}개 / "
        f"새 글 {len(new_ids)}개 / "
        f"본문 열람 {len(new_candidates)}개"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TimeoutError as exc:
        print(f"브라우저 시간 초과: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print(f"GM패왕 자동 수집 실패: {exc}", file=sys.stderr)
        raise SystemExit(1)
