from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urljoin

import requests
import yaml

from server.application.hanall_page_items import (
    build_known_events_context,
    classify_event_freshness,
    merge_known_events,
    parse_known_event_kst,
)
from server.core.hanall_news_models import GeneratedKnownEvent, OfficialPageItem
from server.settings import SERVER_DIR
from server.utils import now_kst, smart_truncate

logger = logging.getLogger(__name__)

HANALL_KNOWN_EVENTS_PATH = SERVER_DIR / "hanall_known_events.yaml"
GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
IMMUNOVANT_PRESS_RELEASES_URL = "https://www.immunovant.com/investors/news-events/press-releases"
HANALL_HOME_URL = "https://www.hanall.com/"
HANALL_KRX_FILINGS_URL = "https://www.hanall.com/m54.php"
HANALL_IR_EVENTS_URL = "https://www.hanall.com/m52.php"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK0001764013.json"

LOOKBACK_HOURS = 24
HTTP_TIMEOUT_SECONDS = 12
MAX_PACKET_CHARS = 8500
MAX_SNIPPET_CHARS = 360
MAX_ITEMS_TOTAL = 12
USER_AGENT = "kakao-bot/1.0 (hanall monitoring)"
HANALL_DIRECT_COMPANIES = ("HanAll Biopharma", "Immunovant")
HANALL_COMPANY_ALIASES = ("HanAll Biopharma", "한올바이오파마", "Immunovant", "IMVT")
HANALL_CORE_ASSETS = (
    "batoclimab",
    "HL161",
    "IMVT-1401",
    "RVT-1401",
    "HBM9161",
    "IMVT-1402",
    "HL161ANS",
    "tanfanercept",
    "HL036",
)
HANALL_KEY_INDICATIONS = ("MG/gMG", "TED", "CIDP", "GD", "D2T RA/RA", "SjD", "CLE", "DED")
HANALL_COMPETITOR_SEED_THEMES = ("FcRn", "TED", "dry eye disease")
HANALL_ASSET_ALIAS_RULES = (
    "batoclimab = HL161 / IMVT-1401 / RVT-1401 / HBM9161",
    "IMVT-1402 = HL161ANS",
    "tanfanercept = HL036",
)
HANALL_FINAL_SECTION_HEADINGS = (
    "요약",
    "오늘 예정 이벤트",
    "Confirmed Updates — Company Direct",
    "Confirmed Updates — Competitor Relevant",
    "Competitor Map Snapshot",
    "Checked Source Log",
    "Unverified Leads",
    "Coverage Gaps",
    "Omission Audit",
    "검증 메모",
)

DIRECT_TERMS = (
    "hanall",
    "hanall biopharma",
    "한올바이오파마",
    "immunovant",
    "imvt",
    "batoclimab",
    "hl161",
    "imvt-1401",
    "rvt-1401",
    "hbm9161",
    "imvt-1402",
    "hl161ans",
    "tanfanercept",
    "hl036",
)
COMPETITOR_TERMS = (
    "argenx",
    "efgartigimod",
    "vyvgart",
    "ucb",
    "rozanolixizumab",
    "rystiggo",
    "nipocalimab",
    "johnson & johnson",
    "jnj",
    "harbour biomed",
    "roivant",
    "teprotumumab",
    "amgen",
)
INDICATION_TERMS = (
    "myasthenia",
    "gmg",
    "cidp",
    "graves",
    "thyroid eye",
    "ted",
    "sjogren",
    "sjd",
    "lupus",
    "cle",
    "dry eye",
    "ded",
    "rheumatoid",
    "rheumatoid arthritis",
)
GOOGLE_NEWS_QUERIES = (
    "\"HanAll Biopharma\" OR 한올바이오파마 OR Immunovant OR IMVT when:1d",
    "batoclimab OR \"IMVT-1401\" OR \"RVT-1401\" OR HBM9161 OR \"IMVT-1402\" OR HL161ANS when:1d",
    "(FcRn OR efgartigimod OR rozanolixizumab OR nipocalimab OR teprotumumab) (Immunovant OR HanAll OR myasthenia OR Graves OR TED OR CIDP OR Sjogren) when:1d",
)
SOURCE_PRIORITY = {
    "scheduled_event": 0,
    "company_direct": 1,
    "competitor_relevant": 2,
}


@dataclass(frozen=True)
class HanallResearchItem:
    bucket: str
    entity: str
    source: str
    title: str
    url: str
    published_at: datetime | None
    published_text: str
    snippet: str


@dataclass(frozen=True)
class HanallSourceStatus:
    source: str
    status: str
    checked_at_text: str
    note: str


class _AnchorExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[tuple[str, str]] = []
        self._href: str | None = None
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self._href = value
                self._chunks = []
                return

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._href is None:
            return
        text = _normalize_space(" ".join(self._chunks))
        if text:
            self.items.append((text, self._href))
        self._href = None
        self._chunks = []


class _TextLineExtractor(HTMLParser):
    _BLOCK_TAGS = {
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "header",
        "li",
        "main",
        "ol",
        "p",
        "section",
        "table",
        "tbody",
        "td",
        "th",
        "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return
        if lowered in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript"} and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if lowered in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def lines(self) -> list[str]:
        text = "".join(self._chunks)
        return [
            normalized
            for normalized in (_normalize_space(line) for line in text.splitlines())
            if normalized
        ]


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _normalize_text_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _normalize_space(text).lower())


def _request_text(session: requests.Session, url: str) -> str:
    response = session.get(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
        },
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.text


def _build_http_session() -> requests.Session:
    return requests.Session()


def _extract_anchor_items(html_text: str, base_url: str) -> list[tuple[str, str]]:
    parser = _AnchorExtractor()
    parser.feed(html_text)
    normalized: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for label, href in parser.items:
        url = urljoin(base_url, href)
        pair = (_normalize_space(label), url)
        if not pair[0] or pair in seen:
            continue
        seen.add(pair)
        normalized.append(pair)
    return normalized


def _extract_visible_lines(html_text: str) -> list[str]:
    parser = _TextLineExtractor()
    parser.feed(html_text)
    return parser.lines()


def _extract_article_snippet(html_text: str) -> str:
    lines = _extract_visible_lines(html_text)
    snippets: list[str] = []
    for line in lines:
        if len(line) < 40:
            continue
        lowered = line.lower()
        if lowered.startswith(("privacy policy", "terms of use", "copyright")):
            continue
        if "all rights reserved" in lowered:
            continue
        snippets.append(line)
        if len(snippets) >= 3:
            break
    return smart_truncate(" ".join(snippets), MAX_SNIPPET_CHARS) if snippets else ""


def _parse_kst_date(value: str) -> datetime | None:
    match = re.search(r"(?P<year>\d{4})[.-](?P<month>\d{2})[.-](?P<day>\d{2})", value)
    if not match:
        return None
    return datetime(
        int(match.group("year")),
        int(match.group("month")),
        int(match.group("day")),
        tzinfo=now_kst().tzinfo,
    )


def _parse_us_news_datetime(value: str) -> datetime | None:
    match = re.search(
        r"(?P<month>[A-Z][a-z]{2}) (?P<day>\d{1,2}), (?P<year>\d{4}) "
        r"(?P<hour>\d{1,2}):(?P<minute>\d{2}) (?P<ampm>am|pm) (?P<tz>EST|EDT|UTC|GMT)",
        value,
    )
    if not match:
        return None
    month = datetime.strptime(match.group("month"), "%b").month
    hour = int(match.group("hour"))
    if match.group("ampm") == "pm" and hour != 12:
        hour += 12
    if match.group("ampm") == "am" and hour == 12:
        hour = 0
    tz_map = {
        "EST": timezone(timedelta(hours=-5)),
        "EDT": timezone(timedelta(hours=-4)),
        "UTC": timezone.utc,
        "GMT": timezone.utc,
    }
    return datetime(
        int(match.group("year")),
        month,
        int(match.group("day")),
        hour,
        int(match.group("minute")),
        tzinfo=tz_map[match.group("tz")],
    ).astimezone(now_kst().tzinfo)


def _parse_sec_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(now_kst().tzinfo)
    except ValueError:
        return None


def _format_kst(dt: datetime | None, fallback: str = "확인 불가") -> str:
    if dt is None:
        return fallback
    return dt.astimezone(now_kst().tzinfo).strftime("%Y-%m-%d %H:%M KST")


def _parse_known_event_kst(value: str) -> datetime | None:
    normalized = _normalize_space(value).removesuffix(" KST")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(normalized, fmt)
            return parsed.replace(tzinfo=now_kst().tzinfo)
        except ValueError:
            continue
    return None


def _known_event_is_completed(*, fact: str, status_note: str) -> bool:
    combined = _normalize_space(f"{fact} {status_note}").lower()
    completion_markers = (
        "confirmed",
        "completed",
        "held",
        "filed",
        "발표 완료",
        "개최 완료",
        "완료",
        "완료됨",
        "확정",
        "종료",
    )
    return any(marker in combined for marker in completion_markers)


def _classify_known_event_aging(
    *,
    scheduled_for_kst: str,
    fact: str,
    status_note: str,
    current_now: datetime,
) -> str:
    if _known_event_is_completed(fact=fact, status_note=status_note):
        return "confirmed_or_completed"
    scheduled_at = _parse_known_event_kst(scheduled_for_kst)
    if scheduled_at is None:
        return "due_today"
    localized = scheduled_at.astimezone(current_now.tzinfo)
    if localized.date() > current_now.date():
        return "due_future"
    if localized.date() == current_now.date():
        return "due_today"
    return "past_due_without_followup"


def _normalize_known_event_copy(raw_event: dict[str, Any], *, current_now: datetime) -> dict[str, str]:
    entity = str(raw_event.get("entity", "")).strip() or "-"
    category = str(raw_event.get("category", "")).strip() or "-"
    scheduled_for_kst = str(raw_event.get("scheduled_for_kst", "")).strip() or "-"
    fact = str(raw_event.get("fact", "")).strip() or "-"
    basis = str(raw_event.get("basis", "")).strip() or "-"
    primary_source = str(raw_event.get("primary_source", "")).strip() or "-"
    status_note = str(raw_event.get("status_note", "")).strip() or "-"
    aging_status = _classify_known_event_aging(
        scheduled_for_kst=scheduled_for_kst,
        fact=fact,
        status_note=status_note,
        current_now=current_now,
    )
    if aging_status == "past_due_without_followup":
        fact = f"{entity} 일정 예정일 경과, 후속 공시 확인 필요"
        status_note = "예정일 경과, 후속 공시 확인 필요"
    return {
        "entity": entity,
        "category": category,
        "scheduled_for_kst": scheduled_for_kst,
        "fact": fact,
        "basis": basis,
        "primary_source": primary_source,
        "status_note": status_note,
        "aging_status": aging_status,
    }


def _is_recent(dt: datetime | None, *, window_start: datetime, now: datetime) -> bool:
    if dt is None:
        return False
    localized = dt.astimezone(now.tzinfo)
    return window_start <= localized <= now


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = str(text or "").lower()
    return any(term in lowered for term in terms)


def _classify_bucket(title: str, snippet: str) -> str:
    combined = f"{title}\n{snippet}".lower()
    if _contains_any(combined, DIRECT_TERMS):
        return "company_direct"
    if _contains_any(combined, COMPETITOR_TERMS) or _contains_any(combined, INDICATION_TERMS):
        return "competitor_relevant"
    return "company_direct"


def _build_status(*, source: str, status: str, note: str, checked_at: datetime | None = None) -> HanallSourceStatus:
    current = checked_at or now_kst()
    return HanallSourceStatus(
        source=source,
        status=status,
        checked_at_text=current.strftime("%Y-%m-%d %H:%M KST"),
        note=note,
    )


def _fetch_detail_snippet(session: requests.Session, url: str) -> str:
    try:
        return _extract_article_snippet(_request_text(session, url))
    except Exception as exc:
        logger.info("hanall detail snippet skipped url=%s error=%s", url, exc)
        return ""


def _collect_google_news_items(
    session: requests.Session,
    *,
    window_start: datetime,
    now: datetime,
) -> tuple[list[HanallResearchItem], HanallSourceStatus]:
    collected: list[HanallResearchItem] = []
    seen_urls: set[str] = set()
    try:
        for query in GOOGLE_NEWS_QUERIES:
            url = GOOGLE_NEWS_RSS_URL.format(query=quote_plus(query))
            xml_text = _request_text(session, url)
            for item_block in re.findall(r"<item>(.*?)</item>", xml_text, flags=re.DOTALL | re.IGNORECASE):
                title_match = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", item_block, re.DOTALL)
                link_match = re.search(r"<link>(.*?)</link>", item_block, re.DOTALL)
                pub_match = re.search(r"<pubDate>(.*?)</pubDate>", item_block, re.DOTALL)
                desc_match = re.search(
                    r"<description><!\[CDATA\[(.*?)\]\]></description>|<description>(.*?)</description>",
                    item_block,
                    re.DOTALL,
                )
                title = _normalize_space((title_match.group(1) or title_match.group(2) or "") if title_match else "")
                if not title:
                    continue
                published_at = parsedate_to_datetime(pub_match.group(1)).astimezone(now.tzinfo) if pub_match else None
                if not _is_recent(published_at, window_start=window_start, now=now):
                    continue
                link = _normalize_space(link_match.group(1) if link_match else "")
                if not link or link in seen_urls:
                    continue
                description_html = (desc_match.group(1) or desc_match.group(2) or "") if desc_match else ""
                snippet = _extract_article_snippet(description_html) or smart_truncate(
                    " ".join(_extract_visible_lines(description_html)),
                    MAX_SNIPPET_CHARS,
                )
                bucket = _classify_bucket(title, snippet)
                if bucket == "competitor_relevant" or _contains_any(f"{title}\n{snippet}", DIRECT_TERMS):
                    seen_urls.add(link)
                    collected.append(
                        HanallResearchItem(
                            bucket=bucket,
                            entity="Google News lead",
                            source="google_news_rss",
                            title=title,
                            url=link,
                            published_at=published_at,
                            published_text=_format_kst(published_at),
                            snippet=snippet,
                        )
                    )
        status = _build_status(
            source="google_news_rss",
            status="새 항목 있음" if collected else "확인했으나 신규 없음",
            note=f"queries={len(GOOGLE_NEWS_QUERIES)} items={len(collected)}",
            checked_at=now,
        )
        return collected, status
    except Exception as exc:
        logger.warning("hanall google news collection failed error=%s", exc)
        return [], _build_status(source="google_news_rss", status="접근 제한", note=str(exc), checked_at=now)


def _collect_sec_items(
    session: requests.Session,
    *,
    window_start: datetime,
    now: datetime,
) -> tuple[list[HanallResearchItem], HanallSourceStatus]:
    collected: list[HanallResearchItem] = []
    try:
        payload = requests.get(
            SEC_SUBMISSIONS_URL,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        payload.raise_for_status()
        data = payload.json()
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        filing_dates = recent.get("filingDate", [])
        acceptance_times = recent.get("acceptanceDateTime", [])
        accession_numbers = recent.get("accessionNumber", [])
        primary_docs = recent.get("primaryDocument", [])
        descriptions = recent.get("primaryDocDescription", [])
        for index, form in enumerate(forms):
            accepted_at = _parse_sec_datetime(acceptance_times[index] if index < len(acceptance_times) else "")
            if not _is_recent(accepted_at, window_start=window_start, now=now):
                continue
            accession_number = str(accession_numbers[index] if index < len(accession_numbers) else "").strip()
            primary_doc = str(primary_docs[index] if index < len(primary_docs) else "").strip()
            filing_date = str(filing_dates[index] if index < len(filing_dates) else "").strip()
            description = _normalize_space(descriptions[index] if index < len(descriptions) else "")
            accession_path = accession_number.replace("-", "")
            doc_url = (
                f"https://www.sec.gov/Archives/edgar/data/1764013/{accession_path}/{primary_doc}"
                if accession_path and primary_doc
                else ""
            )
            title = _normalize_space(f"SEC {form} {description}".strip()) or f"SEC {form}"
            snippet = ""
            if doc_url:
                snippet = _fetch_detail_snippet(session, doc_url)
            if not snippet:
                snippet = smart_truncate(
                    _normalize_space(
                        f"Form {form}; filing_date={filing_date or '확인 불가'}; "
                        f"description={description or '설명 없음'}; accession={accession_number or '확인 불가'}"
                    ),
                    MAX_SNIPPET_CHARS,
                )
            collected.append(
                HanallResearchItem(
                    bucket="company_direct",
                    entity="Immunovant",
                    source="sec_submissions",
                    title=title,
                    url=doc_url or SEC_SUBMISSIONS_URL,
                    published_at=accepted_at,
                    published_text=_format_kst(accepted_at, filing_date or "확인 불가"),
                    snippet=snippet,
                )
            )
        status = _build_status(
            source="sec_submissions",
            status="새 항목 있음" if collected else "확인했으나 신규 없음",
            note=f"items={len(collected)}",
            checked_at=now,
        )
        return collected, status
    except Exception as exc:
        logger.warning("hanall SEC collection failed error=%s", exc)
        return [], _build_status(source="sec_submissions", status="접근 제한", note=str(exc), checked_at=now)


def _collect_hanall_home_items(
    session: requests.Session,
    *,
    window_start: datetime,
    now: datetime,
) -> tuple[list[HanallResearchItem], HanallSourceStatus]:
    collected: list[HanallResearchItem] = []
    try:
        html_text = _request_text(session, HANALL_HOME_URL)
        for label, href in _extract_anchor_items(html_text, HANALL_HOME_URL):
            if not label.startswith("NEWS "):
                continue
            match = re.match(
                r"^NEWS (?P<title>.+?)(?: - (?P<snippet>.+?))? (?P<date>\d{4}\.\d{2}\.\d{2})$",
                label,
            )
            if not match:
                continue
            published_at = _parse_kst_date(match.group("date"))
            if not _is_recent(published_at, window_start=window_start, now=now):
                continue
            snippet = match.group("snippet") or _fetch_detail_snippet(session, href)
            collected.append(
                HanallResearchItem(
                    bucket="company_direct",
                    entity="HanAll Biopharma",
                    source="hanall_home_news",
                    title=match.group("title"),
                    url=href,
                    published_at=published_at,
                    published_text=_format_kst(published_at, match.group("date")),
                    snippet=smart_truncate(_normalize_space(snippet), MAX_SNIPPET_CHARS),
                )
            )
        status = _build_status(
            source="hanall_home_news",
            status="새 항목 있음" if collected else "확인했으나 신규 없음",
            note=f"items={len(collected)}",
            checked_at=now,
        )
        return collected, status
    except Exception as exc:
        logger.warning("hanall home news collection failed error=%s", exc)
        return [], _build_status(source="hanall_home_news", status="접근 제한", note=str(exc), checked_at=now)


def _collect_hanall_krx_items(
    session: requests.Session,
    *,
    window_start: datetime,
    now: datetime,
) -> tuple[list[HanallResearchItem], HanallSourceStatus]:
    collected: list[HanallResearchItem] = []
    try:
        html_text = _request_text(session, HANALL_KRX_FILINGS_URL)
        for label, href in _extract_anchor_items(html_text, HANALL_KRX_FILINGS_URL):
            match = re.match(
                r"^(?P<no>\d+)\s+(?P<date>\d{4}\.\d{2}\.\d{2})\s+(?P<title>.+?)\s+VIEW MORE",
                label,
            )
            if not match:
                continue
            published_at = _parse_kst_date(match.group("date"))
            if not _is_recent(published_at, window_start=window_start, now=now):
                continue
            collected.append(
                HanallResearchItem(
                    bucket="company_direct",
                    entity="HanAll Biopharma",
                    source="hanall_krx_filings",
                    title=match.group("title"),
                    url=href,
                    published_at=published_at,
                    published_text=_format_kst(published_at, match.group("date")),
                    snippet=smart_truncate(
                        _normalize_space(f"HanAll KRX filing candidate: {match.group('title')}"),
                        MAX_SNIPPET_CHARS,
                    ),
                )
            )
        status = _build_status(
            source="hanall_krx_filings",
            status="새 항목 있음" if collected else "확인했으나 신규 없음",
            note=f"items={len(collected)}",
            checked_at=now,
        )
        return collected, status
    except Exception as exc:
        logger.warning("hanall KRX collection failed error=%s", exc)
        return [], _build_status(source="hanall_krx_filings", status="접근 제한", note=str(exc), checked_at=now)


def _collect_hanall_ir_events(
    session: requests.Session,
    *,
    now: datetime,
) -> tuple[list[HanallResearchItem], HanallSourceStatus]:
    collected: list[HanallResearchItem] = []
    try:
        lines = _extract_visible_lines(_request_text(session, HANALL_IR_EVENTS_URL))
        for index, line in enumerate(lines):
            if line != "UP COMING":
                continue
            published_at = _parse_kst_date(lines[index - 1] if index > 0 else "")
            title = lines[index + 1] if index + 1 < len(lines) else ""
            if not published_at or not title:
                continue
            if published_at.date() != now.date():
                continue
            collected.append(
                HanallResearchItem(
                    bucket="scheduled_event",
                    entity="HanAll Biopharma",
                    source="hanall_ir_events",
                    title=title,
                    url=HANALL_IR_EVENTS_URL,
                    published_at=published_at,
                    published_text=_format_kst(published_at, lines[index - 1]),
                    snippet=smart_truncate(
                        _normalize_space(f"HanAll investor event candidate: {title}"),
                        MAX_SNIPPET_CHARS,
                    ),
                )
            )
        status = _build_status(
            source="hanall_ir_events",
            status="새 항목 있음" if collected else "확인했으나 신규 없음",
            note=f"today_candidates={len(collected)}",
            checked_at=now,
        )
        return collected, status
    except Exception as exc:
        logger.warning("hanall IR event collection failed error=%s", exc)
        return [], _build_status(source="hanall_ir_events", status="접근 제한", note=str(exc), checked_at=now)


def _collect_immunovant_press_items(
    session: requests.Session,
    *,
    window_start: datetime,
    now: datetime,
) -> tuple[list[HanallResearchItem], HanallSourceStatus]:
    collected: list[HanallResearchItem] = []
    try:
        html_text = _request_text(session, IMMUNOVANT_PRESS_RELEASES_URL)
        href_map = {
            _normalize_text_key(label): href
            for label, href in _extract_anchor_items(html_text, IMMUNOVANT_PRESS_RELEASES_URL)
            if "/detail/" in href and len(label) >= 20
        }
        lines = _extract_visible_lines(html_text)
        for index, line in enumerate(lines):
            published_at = _parse_us_news_datetime(line)
            if not _is_recent(published_at, window_start=window_start, now=now):
                continue
            title = ""
            for candidate in lines[index + 1 : index + 4]:
                if len(candidate) >= 20:
                    title = candidate
                    break
            if not title:
                continue
            url = href_map.get(_normalize_text_key(title), IMMUNOVANT_PRESS_RELEASES_URL)
            snippet = _fetch_detail_snippet(session, url)
            collected.append(
                HanallResearchItem(
                    bucket="company_direct",
                    entity="Immunovant",
                    source="immunovant_press_releases",
                    title=title,
                    url=url,
                    published_at=published_at,
                    published_text=_format_kst(published_at, line),
                    snippet=snippet or smart_truncate(title, MAX_SNIPPET_CHARS),
                )
            )
        status = _build_status(
            source="immunovant_press_releases",
            status="새 항목 있음" if collected else "확인했으나 신규 없음",
            note=f"items={len(collected)}",
            checked_at=now,
        )
        return collected, status
    except Exception as exc:
        logger.warning("immunovant press release collection failed error=%s", exc)
        return [], _build_status(source="immunovant_press_releases", status="접근 제한", note=str(exc), checked_at=now)


def _load_hanall_known_event_overrides(path: Path = HANALL_KNOWN_EVENTS_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as file:
        loaded = yaml.safe_load(file) or {}
    if not isinstance(loaded, dict):
        return []
    events = loaded.get("known_events", [])
    return list(events) if isinstance(events, list) else []


def get_hanall_known_events(
    as_of_date: str | None = None,
    *,
    current_now: datetime | None = None,
    generated_events: list[GeneratedKnownEvent] | None = None,
) -> list[dict[str, str]]:
    normalized_date = str(as_of_date or "").strip()[:10] or now_kst().strftime("%Y-%m-%d")
    reference_now = current_now or now_kst()
    events: list[dict[str, str]] = []
    merged_events = merge_known_events(
        _load_hanall_known_event_overrides(),
        generated_events or [],
        current_now=reference_now,
    )
    for merged_event in merged_events:
        normalized = merged_event.model_dump(mode="json")
        scheduled_for_kst = normalized.get("scheduled_for_kst", "-")
        scheduled_dt = parse_known_event_kst(scheduled_for_kst)
        if scheduled_dt is not None and scheduled_dt.astimezone(reference_now.tzinfo).date().isoformat() > normalized_date:
            continue
        freshness_status = classify_event_freshness(
            scheduled_for_kst=scheduled_for_kst,
            fact=normalized.get("fact", "-"),
            status_note=normalized.get("status_note", "-"),
            current_now=reference_now,
        )
        if freshness_status == "stale":
            normalized["aging_status"] = "past_due_without_followup"
        elif freshness_status == "completed_unknown":
            normalized["aging_status"] = "confirmed_or_completed"
        elif freshness_status == "due_today":
            normalized["aging_status"] = "due_today"
        else:
            normalized["aging_status"] = "due_future" if scheduled_dt is not None else "due_today"
        events.append(normalized)
    events.sort(key=lambda item: item.get("scheduled_for_kst", ""))
    return events


def build_hanall_known_events_context(
    as_of_date: str | None = None,
    *,
    current_now: datetime | None = None,
    generated_events: list[GeneratedKnownEvent] | None = None,
) -> str:
    return build_known_events_context(
        get_hanall_known_events(as_of_date, current_now=current_now, generated_events=generated_events)
    )


def build_hanall_scope_context() -> str:
    return "\n".join(
        [
            "watch_scope:",
            f"- direct_companies: {', '.join(HANALL_DIRECT_COMPANIES)}",
            f"- company_aliases: {', '.join(HANALL_COMPANY_ALIASES)}",
            f"- core_assets: {', '.join(HANALL_CORE_ASSETS)}",
            f"- asset_aliases: {'; '.join(HANALL_ASSET_ALIAS_RULES)}",
            f"- key_indications: {', '.join(HANALL_KEY_INDICATIONS)}",
            f"- indication_keywords: {', '.join(INDICATION_TERMS)}",
            f"- competitor_seed_themes: {', '.join(HANALL_COMPETITOR_SEED_THEMES)}",
            f"- competitor_alias_keywords: {', '.join(COMPETITOR_TERMS)}",
            "- competitor_definition_rule: include only read-through updates that are directly relevant to HanAll/Immunovant assets or target indications",
        ]
    )


def build_hanall_common_rules_context() -> str:
    return "\n".join(
        [
            "common_rules:",
            "- timezone_rule: always interpret and render dates/times in Asia/Seoul using YYYY-MM-DD HH:MM KST",
            "- fact_priority_rule: confirmed facts from official APIs take precedence over RSS hits and later search results",
            "- search_rule: search is only for omission fill and must not overwrite confirmed official facts",
            "- confirmation_rule: keep confirmed updates, follow-up-needed items, still-unchecked areas, and omission review separate",
            "- empty_state_rule: if a section is empty, render '- 없음' rather than implying no update was verified",
            "- wording_rule: do not replace '확인 불가' with '업데이트 없음'",
        ]
    )


def build_hanall_final_output_context() -> str:
    section_lines = [f"  {index}. {heading}" for index, heading in enumerate(HANALL_FINAL_SECTION_HEADINGS, start=1)]
    return "\n".join(
        [
            "final_output_contract:",
            "- header_rule: start with [한올/Immunovant 24시간 브리핑]",
            "- metadata_rule: include 기준, 범위, 커버리지, 확인 이벤트, 오늘 예정 이벤트 count before the required sections",
            "- plain_text_only: no table, JSON, code block, or markdown decoration",
            "- messenger_readability: keep short paragraphs and line breaks that read well in KakaoTalk",
            "- empty_section_rule: every required section must appear and empty sections must contain '- 없음'",
            "- final_section_order:",
            *section_lines,
        ]
    )


def build_hanall_base_prompt_replacements(current_now: datetime | None = None) -> dict[str, str]:
    now = current_now or now_kst()
    window_start = now - timedelta(hours=LOOKBACK_HOURS)
    return {
        "__NOW_KST__": now.strftime("%Y-%m-%d %H:%M KST"),
        "__TODAY_KST__": now.strftime("%Y-%m-%d"),
        "__SCHEDULE_LOOKBACK_START_KST__": window_start.strftime("%Y-%m-%d %H:%M KST"),
        "__SCHEDULE_LOOKBACK_END_KST__": now.strftime("%Y-%m-%d %H:%M KST"),
        "__KNOWN_EVENTS_CONTEXT__": build_hanall_known_events_context(now.strftime("%Y-%m-%d"), current_now=now),
        "__HANALL_SCOPE_CONTEXT__": build_hanall_scope_context(),
        "__HANALL_COMMON_RULES_CONTEXT__": build_hanall_common_rules_context(),
        "__HANALL_FINAL_OUTPUT_CONTEXT__": build_hanall_final_output_context(),
    }


def _dedupe_items(items: list[HanallResearchItem]) -> list[HanallResearchItem]:
    deduped: list[HanallResearchItem] = []
    seen: set[str] = set()
    for item in items:
        key = item.url or f"{item.source}:{_normalize_text_key(item.title)}:{item.published_text}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _select_packet_items(items: list[HanallResearchItem]) -> list[HanallResearchItem]:
    ordered = sorted(
        _dedupe_items(items),
        key=lambda item: (
            SOURCE_PRIORITY.get(item.bucket, 99),
            -(item.published_at.timestamp() if item.published_at else 0),
            item.source,
        ),
    )
    selected: list[HanallResearchItem] = []
    bucket_counts: dict[str, int] = {}
    bucket_limits = {
        "scheduled_event": 3,
        "company_direct": 6,
        "competitor_relevant": 4,
    }
    for item in ordered:
        if len(selected) >= MAX_ITEMS_TOTAL:
            break
        if bucket_counts.get(item.bucket, 0) >= bucket_limits.get(item.bucket, 3):
            continue
        selected.append(item)
        bucket_counts[item.bucket] = bucket_counts.get(item.bucket, 0) + 1
    return selected


def build_hanall_research_packet(current_now: datetime | None = None) -> str:
    now = current_now or now_kst()
    window_start = now - timedelta(hours=LOOKBACK_HOURS)
    session = _build_http_session()
    try:
        collected_items: list[HanallResearchItem] = []
        statuses: list[HanallSourceStatus] = []

        for collector in (
            _collect_google_news_items,
            _collect_sec_items,
            _collect_hanall_home_items,
            _collect_hanall_krx_items,
            _collect_immunovant_press_items,
        ):
            items, status = collector(session, window_start=window_start, now=now)
            collected_items.extend(items)
            statuses.append(status)

        ir_event_items, ir_event_status = _collect_hanall_ir_events(session, now=now)
        collected_items.extend(ir_event_items)
        statuses.append(ir_event_status)

        selected_items = _select_packet_items(collected_items)
        omitted_count = max(0, len(_dedupe_items(collected_items)) - len(selected_items))
        lines = [
            "[Stage 1 Research Packet]",
            f"generated_at: {now.strftime('%Y-%m-%d %H:%M KST')}",
            f"window: {window_start.strftime('%Y-%m-%d %H:%M KST')} ~ {now.strftime('%Y-%m-%d %H:%M KST')}",
            "scope:",
            "- direct_companies: HanAll Biopharma, Immunovant",
            "- core_assets: batoclimab/HL161/IMVT-1401/RVT-1401/HBM9161, IMVT-1402/HL161ANS, tanfanercept/HL036",
            "- key_indications: MG/gMG, TED, CIDP, GD, D2T RA/RA, SjD, CLE, DED",
            "- competitor_seed_themes: FcRn, TED, dry eye disease",
            "today_known_events:",
            build_hanall_known_events_context(now.strftime("%Y-%m-%d"), current_now=now),
            "candidate_signals:",
        ]
        if not selected_items:
            lines.append("- none")
        for index, item in enumerate(selected_items, start=1):
            lines.extend(
                [
                    f"{index}) bucket={item.bucket} | entity={item.entity} | source={item.source} | published={item.published_text}",
                    f"title={item.title}",
                    f"url={item.url or '-'}",
                    f"snippet={smart_truncate(_normalize_space(item.snippet or item.title), MAX_SNIPPET_CHARS)}",
                ]
            )
        if omitted_count:
            lines.append(f"omitted_for_token_control: {omitted_count}")
        lines.append("source_status:")
        for status in statuses:
            lines.append(
                f"- {status.source}: {status.status} | checked={status.checked_at_text} | note={status.note or '-'}"
            )

        packet = "\n".join(lines).strip()
        if len(packet) <= MAX_PACKET_CHARS:
            return packet

        trimmed_lines = lines[:10]
        trimmed_lines.append("candidate_signals:")
        compact_items = selected_items[:6]
        if not compact_items:
            trimmed_lines.append("- none")
        for index, item in enumerate(compact_items, start=1):
            trimmed_lines.extend(
                [
                    f"{index}) bucket={item.bucket} | entity={item.entity} | source={item.source} | published={item.published_text}",
                    f"title={item.title}",
                    f"url={item.url or '-'}",
                    f"snippet={smart_truncate(_normalize_space(item.snippet or item.title), 220)}",
                ]
            )
        trimmed_lines.append(
            f"omitted_for_token_control: {max(0, len(_dedupe_items(collected_items)) - len(compact_items))}"
        )
        trimmed_lines.append("source_status:")
        trimmed_lines.extend(
            f"- {status.source}: {status.status} | checked={status.checked_at_text} | note={status.note or '-'}"
            for status in statuses
        )
        return "\n".join(trimmed_lines).strip()
    finally:
        session.close()


def build_hanall_prompt_replacements(current_now: datetime | None = None) -> dict[str, str]:
    now = current_now or now_kst()
    replacements = build_hanall_base_prompt_replacements(now)
    replacements["__HANALL_RESEARCH_PACKET__"] = build_hanall_research_packet(now)
    return replacements
