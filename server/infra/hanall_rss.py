from __future__ import annotations

import logging
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree

import requests

from server.config import get_hanall_sources_config
from server.core.hanall_news_models import CheckedSourceLogEntry, CoverageGap, RSSCollectionResult, RSSItem
from server.utils import now_kst, smart_truncate

logger = logging.getLogger(__name__)

USER_AGENT = "kakao-bot/1.0 (hanall rss)"


def _child_text(node: ElementTree.Element, tag_names: tuple[str, ...]) -> str:
    for tag_name in tag_names:
        child = node.find(tag_name)
        if child is not None and child.text:
            text = child.text.strip()
            if text:
                return text
    return ""


def _parse_published_at(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return parsedate_to_datetime(text).astimezone(now_kst().tzinfo)
    except (TypeError, ValueError):
        return None


def _build_source_log(
    *,
    source_name: str,
    status: str,
    note: str,
    endpoint: str,
    http_status: int | None = None,
    checked_at: datetime | None = None,
) -> CheckedSourceLogEntry:
    current = checked_at or now_kst()
    return CheckedSourceLogEntry(
        source_family="rss",
        source_name=source_name,
        status=status,
        checked_at_kst=current.strftime("%Y-%m-%d %H:%M KST"),
        note=note,
        endpoint=endpoint,
        http_status=http_status,
    )


def _build_gap(
    *,
    source_name: str,
    gap_type: str,
    detail: str,
    endpoint: str,
    severity: str = "medium",
    http_status: int | None = None,
) -> CoverageGap:
    return CoverageGap(
        source_family="rss",
        source_name=source_name,
        gap_type=gap_type,
        detail=detail,
        severity=severity,
        endpoint=endpoint,
        http_status=http_status,
    )


def _iter_rss_items(root: ElementTree.Element) -> list[ElementTree.Element]:
    return root.findall(".//item") + root.findall(".//{http://www.w3.org/2005/Atom}entry")


def _extract_item_payload(item: ElementTree.Element) -> tuple[str, str, str, datetime | None]:
    title = _child_text(item, ("title", "{http://www.w3.org/2005/Atom}title"))
    summary = _child_text(
        item,
        ("description", "summary", "{http://www.w3.org/2005/Atom}summary"),
    )
    link = _child_text(item, ("link", "{http://www.w3.org/2005/Atom}link"))
    if not link:
        atom_link = item.find("{http://www.w3.org/2005/Atom}link")
        if atom_link is not None:
            link = str(atom_link.attrib.get("href", "")).strip()
    published_text = _child_text(
        item,
        ("pubDate", "published", "updated", "{http://www.w3.org/2005/Atom}published", "{http://www.w3.org/2005/Atom}updated"),
    )
    published_at = _parse_published_at(published_text)
    return title, summary, link, published_at


def fetch_hanall_rss_results(
    *,
    session: requests.Session | None = None,
    current_now: datetime | None = None,
) -> RSSCollectionResult:
    now = current_now or now_kst()
    config = get_hanall_sources_config()
    rss_config = config.get("rss", {}) if isinstance(config.get("rss"), dict) else {}
    if not bool(rss_config.get("enabled", True)):
        return RSSCollectionResult(
            checked_feed_count=0,
            checked_source_log=[
                _build_source_log(
                    source_name="rss",
                    status="disabled",
                    note="rss collector disabled by config",
                    endpoint="-",
                    checked_at=now,
                )
            ],
        )

    timeout_seconds = int(rss_config.get("timeout_seconds", 10) or 10)
    max_items_per_feed = int(rss_config.get("max_items_per_feed", 5) or 5)
    feeds = rss_config.get("feeds", []) if isinstance(rss_config.get("feeds"), list) else []
    owned_session = session is None
    client = session or requests.Session()
    items: list[RSSItem] = []
    checked_source_log: list[CheckedSourceLogEntry] = []
    coverage_gaps: list[CoverageGap] = []

    try:
        for raw_feed in feeds:
            if not isinstance(raw_feed, dict) or not bool(raw_feed.get("enabled", True)):
                continue
            feed_name = str(raw_feed.get("name", "rss_feed")).strip() or "rss_feed"
            endpoint = str(raw_feed.get("url", "")).strip()
            category = str(raw_feed.get("category", "rss")).strip() or "rss"
            if not endpoint:
                coverage_gaps.append(
                    _build_gap(
                        source_name=feed_name,
                        gap_type="invalid_config",
                        detail="rss feed url missing",
                        endpoint="-",
                        severity="low",
                    )
                )
                checked_source_log.append(
                    _build_source_log(
                        source_name=feed_name,
                        status="invalid_config",
                        note="rss feed url missing",
                        endpoint="-",
                        checked_at=now,
                    )
                )
                continue

            try:
                response = client.get(
                    endpoint,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, text/xml"},
                    timeout=timeout_seconds,
                )
                response.raise_for_status()
                root = ElementTree.fromstring(response.text)
            except requests.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                logger.warning("hanall rss http error feed=%s status=%s error=%s", feed_name, status_code, exc)
                coverage_gaps.append(
                    _build_gap(
                        source_name=feed_name,
                        gap_type="http_error",
                        detail=str(exc),
                        endpoint=endpoint,
                        http_status=status_code,
                    )
                )
                checked_source_log.append(
                    _build_source_log(
                        source_name=feed_name,
                        status="http_error",
                        note=str(exc),
                        endpoint=endpoint,
                        http_status=status_code,
                        checked_at=now,
                    )
                )
                continue
            except (requests.RequestException, ElementTree.ParseError) as exc:
                logger.warning("hanall rss fetch failed feed=%s error=%s", feed_name, exc)
                coverage_gaps.append(
                    _build_gap(
                        source_name=feed_name,
                        gap_type="fetch_error",
                        detail=str(exc),
                        endpoint=endpoint,
                    )
                )
                checked_source_log.append(
                    _build_source_log(
                        source_name=feed_name,
                        status="fetch_error",
                        note=str(exc),
                        endpoint=endpoint,
                        checked_at=now,
                    )
                )
                continue

            feed_items = _iter_rss_items(root)[:max_items_per_feed]
            for node in feed_items:
                title, summary, link, published_at = _extract_item_payload(node)
                if not title:
                    continue
                items.append(
                    RSSItem(
                        feed_name=feed_name,
                        category=category,
                        title=title,
                        summary=smart_truncate(summary, 400),
                        published_at=published_at,
                        url=link or endpoint,
                    )
                )

            checked_source_log.append(
                _build_source_log(
                    source_name=feed_name,
                    status="checked",
                    note=f"items={len(feed_items)}",
                    endpoint=endpoint,
                    checked_at=now,
                )
            )

        return RSSCollectionResult(
            checked_feed_count=len(checked_source_log),
            items=items,
            checked_source_log=checked_source_log,
            coverage_gaps=coverage_gaps,
        )
    finally:
        if owned_session:
            client.close()
