from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.errors import ExternalAPIError
from app.repositories import OptionsSentimentSummaryRepository
from app.schemas import OptionsPCRDailySummary, OptionsSentimentCollectionResult, OptionsSentimentSnapshot

logger = logging.getLogger(__name__)


class OptionsSentimentService:
    DEFAULT_SYMBOL = "IMVT"
    SOURCE = "tradier"

    QUALITY_OK = "OK"
    QUALITY_LOW_VOLUME = "LOW_VOLUME"
    QUALITY_LOW_OI = "LOW_OI"
    QUALITY_SINGLE_EXPIRY_DISTORTION = "SINGLE_EXPIRY_DISTORTION"
    QUALITY_PARTIAL_CHAIN = "PARTIAL_CHAIN"
    QUALITY_ERROR = "ERROR"

    NO_PUBLISH_SANDBOX = "SANDBOX"
    NO_PUBLISH_PARTIAL_CHAIN = "PARTIAL_CHAIN"
    NO_PUBLISH_LOW_OI = "LOW_OI"
    NO_PUBLISH_SINGLE_EXPIRY_DISTORTION = "SINGLE_EXPIRY_DISTORTION"
    NO_PUBLISH_CALL_OI_ZERO = "CALL_OI_ZERO"
    NO_PUBLISH_CALL_VOL_ZERO = "CALL_VOL_ZERO"
    NO_PUBLISH_PCR_UNAVAILABLE = "PCR_UNAVAILABLE"
    NO_PUBLISH_MISSING_REQUIRED_PUBLIC_FIELDS = "MISSING_REQUIRED_PUBLIC_FIELDS"
    WARNING_COLLECTION_FAILED = "COLLECTION_FAILED"
    WARNING_PUBLIC_DELIVERY_FAILED = "PUBLIC_MESSAGE_DELIVERY_FAILED"

    QUALITY_PRIORITY = (
        QUALITY_ERROR,
        QUALITY_PARTIAL_CHAIN,
        QUALITY_SINGLE_EXPIRY_DISTORTION,
        QUALITY_LOW_OI,
        QUALITY_LOW_VOLUME,
        QUALITY_OK,
    )
    PUBLIC_REQUIRED_FIELDS = (
        "date_us",
        "date_kst",
        "symbol",
        "pcr_oi_total",
        "pcr_vol_total",
        "put_oi_total",
        "call_oi_total",
        "put_vol_total",
        "call_vol_total",
        "total_option_volume",
        "total_option_oi",
        "data_quality_flag",
        "source",
        "source_environment",
    )

    def __init__(
        self,
        settings: Settings,
        summary_repository: OptionsSentimentSummaryRepository | None = None,
    ) -> None:
        self._settings = settings
        self._summary_repository = summary_repository
        self._us_timezone = ZoneInfo("America/New_York")

    def is_enabled(self) -> bool:
        return bool(self._settings.tradier_api)

    async def collect_daily_summary(
        self,
        *,
        artifact_date_kst: date,
        symbol: str = DEFAULT_SYMBOL,
    ) -> OptionsSentimentCollectionResult:
        retrieved_at_utc = datetime.now(timezone.utc)
        date_us = retrieved_at_utc.astimezone(self._us_timezone).date()
        if not self.is_enabled():
            snapshot = OptionsSentimentSnapshot(
                collect_status="disabled",
                symbol=symbol,
                date_us=date_us,
                date_kst=artifact_date_kst,
                source=self.SOURCE,
                source_environment=self._settings.tradier_env,
                retrieved_at_utc=retrieved_at_utc,
                should_publish_public=False,
                reason="TRADIER_API is not configured",
            )
            return OptionsSentimentCollectionResult(collect_status="disabled", snapshot=snapshot)

        try:
            summary = await self._collect_summary(
                artifact_date_kst=artifact_date_kst,
                date_us=date_us,
                retrieved_at_utc=retrieved_at_utc,
                symbol=symbol,
            )
            snapshot = self._build_success_snapshot(summary)
            return OptionsSentimentCollectionResult(
                collect_status="success",
                summary=summary,
                snapshot=snapshot,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("options_sentiment_collect_failed", exc_info=exc, extra={"symbol": symbol})
            snapshot = OptionsSentimentSnapshot(
                collect_status="failed",
                symbol=symbol,
                date_us=date_us,
                date_kst=artifact_date_kst,
                source=self.SOURCE,
                source_environment=self._settings.tradier_env,
                retrieved_at_utc=retrieved_at_utc,
                should_publish_public=False,
                reason=str(exc),
            )
            return OptionsSentimentCollectionResult(collect_status="failed", snapshot=snapshot)

    def save_daily_summary(self, summary: OptionsPCRDailySummary) -> OptionsPCRDailySummary:
        if self._summary_repository is None:
            return summary
        return self._summary_repository.save_daily_summary(summary)

    def render_public_message(self, summary: OptionsPCRDailySummary) -> str:
        lines = [
            "[IMVT 옵션 심리 요약]",
            f"- 기준일: US {summary.date_us.isoformat()} / KST {summary.date_kst.isoformat()}",
        ]
        quote_line = self._build_quote_line(summary)
        if quote_line is not None:
            lines.append(quote_line)
        lines.append(
            f"- 전체 OI PCR: {self._format_ratio(summary.pcr_oi_total)} "
            f"(Put OI {summary.put_oi_total:,} / Call OI {summary.call_oi_total:,})"
        )
        lines.append(
            f"- 전체 Volume PCR: {self._format_ratio(summary.pcr_vol_total)} "
            f"(Put Vol {summary.put_vol_total:,} / Call Vol {summary.call_vol_total:,})"
        )
        lines.append(f"- 총 옵션 거래량: {summary.total_option_volume:,} / 총 OI: {summary.total_option_oi:,}")
        if summary.short_dte_pcr_oi is not None:
            lines.append(
                "- 단기 만기권 OI PCR(7~45일 남은 만기 합산): "
                f"{self._format_ratio(summary.short_dte_pcr_oi)}"
            )
        if summary.short_dte_pcr_vol is not None:
            lines.append(
                "- 단기 만기권 Volume PCR(7~45일 남은 만기 합산): "
                f"{self._format_ratio(summary.short_dte_pcr_vol)}"
            )
        if summary.data_quality_flag == self.QUALITY_LOW_VOLUME:
            lines.append("- 오늘 옵션 거래량이 적어 Volume PCR의 방향성 해석은 보류합니다.")
            return "\n".join(lines)

        lines.extend(
            [
                "[해석]",
                f"- OI 기준: {self._describe_ratio(summary.pcr_oi_total, default='중립권')}",
                f"- Volume 기준: {self._describe_ratio(summary.pcr_vol_total, default='중립권')}",
            ]
        )
        return "\n".join(lines)

    def get_warning_reason_from_summary(self, summary: OptionsPCRDailySummary) -> str | None:
        if summary.should_publish_public:
            return None
        if summary.no_publish_reason:
            return summary.no_publish_reason
        if summary.data_quality_flag in {
            self.QUALITY_PARTIAL_CHAIN,
            self.QUALITY_LOW_OI,
            self.QUALITY_SINGLE_EXPIRY_DISTORTION,
            self.QUALITY_ERROR,
        }:
            return summary.data_quality_flag
        return None

    def get_warning_reason_from_snapshot(self, snapshot: OptionsSentimentSnapshot) -> str | None:
        if snapshot.collect_status == "disabled":
            return None
        if snapshot.collect_status == "failed":
            return self.WARNING_COLLECTION_FAILED
        if snapshot.no_publish_reason:
            return snapshot.no_publish_reason
        if snapshot.data_quality_flag in {
            self.QUALITY_PARTIAL_CHAIN,
            self.QUALITY_LOW_OI,
            self.QUALITY_SINGLE_EXPIRY_DISTORTION,
            self.QUALITY_ERROR,
        }:
            return snapshot.data_quality_flag
        return None

    def render_admin_warning_from_summary(
        self,
        summary: OptionsPCRDailySummary,
        *,
        room_name: str | None = None,
        public_message_included: bool = False,
    ) -> str | None:
        reason = self.get_warning_reason_from_summary(summary)
        if reason is None:
            return None
        return self._build_admin_warning_message(
            symbol=summary.symbol,
            date_us=summary.date_us,
            date_kst=summary.date_kst,
            source_environment=summary.source_environment,
            reason=reason,
            detail=self._describe_warning_reason(reason, public_message_included=public_message_included),
            room_name=room_name,
        )

    def render_admin_warning_from_snapshot(
        self,
        snapshot: OptionsSentimentSnapshot,
        *,
        room_name: str | None = None,
    ) -> str | None:
        reason = self.get_warning_reason_from_snapshot(snapshot)
        if reason is None:
            return None
        detail = snapshot.reason if reason == self.WARNING_COLLECTION_FAILED else self._describe_warning_reason(reason)
        return self._build_admin_warning_message(
            symbol=snapshot.symbol,
            date_us=snapshot.date_us,
            date_kst=snapshot.date_kst,
            source_environment=snapshot.source_environment,
            reason=reason,
            detail=detail or self._describe_warning_reason(reason),
            room_name=room_name,
        )

    def render_public_delivery_failure_warning(
        self,
        summary: OptionsPCRDailySummary,
        *,
        room_name: str,
        error_message: str,
    ) -> str:
        return self._build_admin_warning_message(
            symbol=summary.symbol,
            date_us=summary.date_us,
            date_kst=summary.date_kst,
            source_environment=summary.source_environment,
            reason=self.WARNING_PUBLIC_DELIVERY_FAILED,
            detail=error_message,
            room_name=room_name,
        )

    async def _collect_summary(
        self,
        *,
        artifact_date_kst: date,
        date_us: date,
        retrieved_at_utc: datetime,
        symbol: str,
    ) -> OptionsPCRDailySummary:
        async with self._create_client() as client:
            expirations_payload = await self._request_json(
                client,
                "/markets/options/expirations",
                params={"symbol": symbol, "includeAllRoots": "true", "strikes": "false"},
            )
            expirations = self._extract_expirations(expirations_payload)
            if not expirations:
                raise ExternalAPIError("Tradier returned no option expirations")

            by_expiry_rows: list[dict[str, Any]] = []
            failed_expiries: list[dict[str, str]] = []
            for expiration in expirations:
                try:
                    chain_payload = await self._request_json(
                        client,
                        "/markets/options/chains",
                        params={"symbol": symbol, "expiration": expiration.isoformat(), "greeks": "false"},
                    )
                    option_rows = self._extract_option_rows(chain_payload)
                    by_expiry_rows.append(self._aggregate_expiry(expiration, date_us, option_rows))
                except Exception as exc:  # noqa: BLE001
                    failed_expiries.append({"expiration": expiration.isoformat(), "reason": str(exc)})
            if not by_expiry_rows:
                raise ExternalAPIError("Tradier option chains could not be aggregated")

            totals = self._aggregate_totals(by_expiry_rows)
            previous_summary = None
            if self._summary_repository is not None:
                previous_summary = self._summary_repository.get_previous_summary(
                    symbol,
                    before_date_us=date_us,
                    source_environment=self._settings.tradier_env,
                )
            close, change_1d_pct, quote_meta = await self._fetch_quote(client, symbol)
            quality_flags = self._collect_quality_flags(
                by_expiry_rows=by_expiry_rows,
                totals=totals,
                failed_expiries=failed_expiries,
            )
            data_quality_flag = self._select_quality_flag(quality_flags)
            missing_public_fields = self._missing_public_fields(
                symbol=symbol,
                date_us=date_us,
                date_kst=artifact_date_kst,
                totals=totals,
                data_quality_flag=data_quality_flag,
            )
            should_publish_public, no_publish_reason = self._determine_publishability(
                data_quality_flag=data_quality_flag,
                totals=totals,
                missing_public_fields=missing_public_fields,
            )

            raw_response_json = {
                "expirations_requested": [expiration.isoformat() for expiration in expirations],
                "failed_expiries": failed_expiries,
                "quality_flags": sorted(quality_flags),
                "quote": quote_meta,
                "previous_pcr_oi_total": previous_summary.pcr_oi_total if previous_summary is not None else None,
                "previous_pcr_oi_total_delta": self._safe_delta(
                    totals["pcr_oi_total"],
                    previous_summary.pcr_oi_total if previous_summary is not None else None,
                ),
            }

        return OptionsPCRDailySummary(
            date_us=date_us,
            date_kst=artifact_date_kst,
            symbol=symbol,
            close=close,
            change_1d_pct=change_1d_pct,
            pcr_oi_total=totals["pcr_oi_total"],
            pcr_vol_total=totals["pcr_vol_total"],
            put_oi_total=totals["put_oi_total"],
            call_oi_total=totals["call_oi_total"],
            put_vol_total=totals["put_vol_total"],
            call_vol_total=totals["call_vol_total"],
            total_option_volume=totals["total_option_volume"],
            total_option_oi=totals["total_option_oi"],
            short_dte_pcr_oi=totals["short_dte_pcr_oi"],
            short_dte_pcr_vol=totals["short_dte_pcr_vol"],
            data_quality_flag=data_quality_flag,
            should_publish_public=should_publish_public,
            no_publish_reason=no_publish_reason,
            source=self.SOURCE,
            source_environment=self._settings.tradier_env,
            retrieved_at_utc=retrieved_at_utc,
            oi_effective_date=None,
            by_expiry_json=by_expiry_rows,
            raw_response_json=raw_response_json,
        )

    def _create_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url(),
            timeout=self._settings.tradier_timeout_seconds,
            headers={
                "Authorization": f"Bearer {self._settings.tradier_api}",
                "Accept": "application/json",
            },
        )

    def _base_url(self) -> str:
        if self._settings.tradier_env == "live":
            return self._settings.tradier_live_base_url.rstrip("/")
        return self._settings.tradier_sandbox_base_url.rstrip("/")

    async def _request_json(self, client: httpx.AsyncClient, path: str, *, params: dict[str, str]) -> dict[str, Any]:
        try:
            response = await client.get(path, params=params)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            raise ExternalAPIError(f"Tradier request failed for {path}: HTTP {status_code}", status_code=status_code) from exc
        except httpx.RequestError as exc:
            raise ExternalAPIError(f"Tradier request failed for {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ExternalAPIError(f"Tradier response for {path} was not a JSON object")
        return payload

    @staticmethod
    def _extract_expirations(payload: dict[str, Any]) -> list[date]:
        expirations = payload.get("expirations")
        if not isinstance(expirations, dict):
            return []
        expiration_values = expirations.get("date")
        if expiration_values is None:
            return []
        if not isinstance(expiration_values, list):
            expiration_values = [expiration_values]
        parsed: list[date] = []
        for raw_value in expiration_values:
            if raw_value is None:
                continue
            parsed.append(date.fromisoformat(str(raw_value)))
        return parsed

    @staticmethod
    def _extract_option_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
        options = payload.get("options")
        if not isinstance(options, dict):
            return []
        rows = options.get("option")
        if rows is None:
            return []
        if isinstance(rows, dict):
            return [rows]
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        return []

    def _aggregate_expiry(self, expiration: date, date_us: date, option_rows: list[dict[str, Any]]) -> dict[str, Any]:
        put_oi_total = 0
        call_oi_total = 0
        put_vol_total = 0
        call_vol_total = 0
        for row in option_rows:
            option_type = str(row.get("option_type") or row.get("type") or "").strip().lower()
            open_interest = self._to_int(row.get("open_interest"))
            volume = self._to_int(row.get("volume"))
            if option_type == "put":
                put_oi_total += open_interest
                put_vol_total += volume
            elif option_type == "call":
                call_oi_total += open_interest
                call_vol_total += volume

        total_option_oi = put_oi_total + call_oi_total
        total_option_volume = put_vol_total + call_vol_total
        return {
            "expiry": expiration.isoformat(),
            "dte": (expiration - date_us).days,
            "put_oi_total": put_oi_total,
            "call_oi_total": call_oi_total,
            "put_vol_total": put_vol_total,
            "call_vol_total": call_vol_total,
            "total_option_oi": total_option_oi,
            "total_option_volume": total_option_volume,
            "pcr_oi": self._safe_ratio(put_oi_total, call_oi_total),
            "pcr_vol": self._safe_ratio(put_vol_total, call_vol_total),
        }

    def _aggregate_totals(self, by_expiry_rows: list[dict[str, Any]]) -> dict[str, Any]:
        put_oi_total = sum(int(row["put_oi_total"]) for row in by_expiry_rows)
        call_oi_total = sum(int(row["call_oi_total"]) for row in by_expiry_rows)
        put_vol_total = sum(int(row["put_vol_total"]) for row in by_expiry_rows)
        call_vol_total = sum(int(row["call_vol_total"]) for row in by_expiry_rows)
        total_option_oi = put_oi_total + call_oi_total
        total_option_volume = put_vol_total + call_vol_total

        short_dte_rows = [row for row in by_expiry_rows if 7 <= int(row["dte"]) <= 45]
        short_put_oi_total = sum(int(row["put_oi_total"]) for row in short_dte_rows)
        short_call_oi_total = sum(int(row["call_oi_total"]) for row in short_dte_rows)
        short_put_vol_total = sum(int(row["put_vol_total"]) for row in short_dte_rows)
        short_call_vol_total = sum(int(row["call_vol_total"]) for row in short_dte_rows)

        for row in by_expiry_rows:
            row["oi_share_pct"] = round((int(row["total_option_oi"]) / total_option_oi) * 100, 2) if total_option_oi else 0.0
            row["volume_share_pct"] = (
                round((int(row["total_option_volume"]) / total_option_volume) * 100, 2) if total_option_volume else 0.0
            )

        return {
            "put_oi_total": put_oi_total,
            "call_oi_total": call_oi_total,
            "put_vol_total": put_vol_total,
            "call_vol_total": call_vol_total,
            "total_option_oi": total_option_oi,
            "total_option_volume": total_option_volume,
            "pcr_oi_total": self._safe_ratio(put_oi_total, call_oi_total),
            "pcr_vol_total": self._safe_ratio(put_vol_total, call_vol_total),
            "short_dte_pcr_oi": self._safe_ratio(short_put_oi_total, short_call_oi_total) if short_dte_rows else None,
            "short_dte_pcr_vol": self._safe_ratio(short_put_vol_total, short_call_vol_total) if short_dte_rows else None,
        }

    async def _fetch_quote(self, client: httpx.AsyncClient, symbol: str) -> tuple[float | None, float | None, dict[str, Any]]:
        try:
            payload = await self._request_json(client, "/markets/quotes", params={"symbols": symbol})
            quotes = payload.get("quotes")
            quote = quotes.get("quote") if isinstance(quotes, dict) else None
            if isinstance(quote, list):
                quote = quote[0] if quote else None
            if not isinstance(quote, dict):
                raise ExternalAPIError("Tradier quote payload did not include a quote object")
            close = self._to_float(quote.get("close") or quote.get("last"))
            change_1d_pct = self._extract_change_pct(quote, close)
            return close, change_1d_pct, {"status": "ok"}
        except Exception as exc:  # noqa: BLE001
            return None, None, {"status": "failed", "reason": str(exc)}

    def _collect_quality_flags(
        self,
        *,
        by_expiry_rows: list[dict[str, Any]],
        totals: dict[str, Any],
        failed_expiries: list[dict[str, str]],
    ) -> set[str]:
        quality_flags: set[str] = set()
        if failed_expiries:
            quality_flags.add(self.QUALITY_PARTIAL_CHAIN)
        if totals["total_option_volume"] < 1000:
            quality_flags.add(self.QUALITY_LOW_VOLUME)
        if totals["total_option_oi"] < 1000:
            quality_flags.add(self.QUALITY_LOW_OI)
        if self._has_single_expiry_distortion(by_expiry_rows, totals):
            quality_flags.add(self.QUALITY_SINGLE_EXPIRY_DISTORTION)
        if not quality_flags:
            quality_flags.add(self.QUALITY_OK)
        return quality_flags

    def _determine_publishability(
        self,
        *,
        data_quality_flag: str,
        totals: dict[str, Any],
        missing_public_fields: list[str],
    ) -> tuple[bool, str | None]:
        if self._settings.tradier_env != "live":
            return False, self.NO_PUBLISH_SANDBOX
        if data_quality_flag == self.QUALITY_PARTIAL_CHAIN:
            return False, self.NO_PUBLISH_PARTIAL_CHAIN
        if data_quality_flag == self.QUALITY_LOW_OI:
            return False, self.NO_PUBLISH_LOW_OI
        if data_quality_flag == self.QUALITY_SINGLE_EXPIRY_DISTORTION:
            return False, self.NO_PUBLISH_SINGLE_EXPIRY_DISTORTION
        if totals["call_oi_total"] <= 0:
            return False, self.NO_PUBLISH_CALL_OI_ZERO
        if totals["call_vol_total"] <= 0:
            return False, self.NO_PUBLISH_CALL_VOL_ZERO
        if totals["pcr_oi_total"] is None or totals["pcr_vol_total"] is None:
            return False, self.NO_PUBLISH_PCR_UNAVAILABLE
        if missing_public_fields:
            return False, self.NO_PUBLISH_MISSING_REQUIRED_PUBLIC_FIELDS
        return True, None

    def _missing_public_fields(
        self,
        *,
        symbol: str,
        date_us: date,
        date_kst: date,
        totals: dict[str, Any],
        data_quality_flag: str,
    ) -> list[str]:
        payload = {
            "date_us": date_us,
            "date_kst": date_kst,
            "symbol": symbol,
            "pcr_oi_total": totals["pcr_oi_total"],
            "pcr_vol_total": totals["pcr_vol_total"],
            "put_oi_total": totals["put_oi_total"],
            "call_oi_total": totals["call_oi_total"],
            "put_vol_total": totals["put_vol_total"],
            "call_vol_total": totals["call_vol_total"],
            "total_option_volume": totals["total_option_volume"],
            "total_option_oi": totals["total_option_oi"],
            "data_quality_flag": data_quality_flag,
            "source": self.SOURCE,
            "source_environment": self._settings.tradier_env,
        }
        return [field_name for field_name in self.PUBLIC_REQUIRED_FIELDS if payload.get(field_name) is None]

    def _build_success_snapshot(self, summary: OptionsPCRDailySummary) -> OptionsSentimentSnapshot:
        compact_summary = {
            "pcr_oi_total": summary.pcr_oi_total,
            "pcr_vol_total": summary.pcr_vol_total,
            "total_option_volume": summary.total_option_volume,
            "total_option_oi": summary.total_option_oi,
            "data_quality_flag": summary.data_quality_flag,
        }
        return OptionsSentimentSnapshot(
            collect_status="success",
            symbol=summary.symbol,
            date_us=summary.date_us,
            date_kst=summary.date_kst,
            source=summary.source,
            source_environment=summary.source_environment,
            retrieved_at_utc=summary.retrieved_at_utc,
            data_quality_flag=summary.data_quality_flag,
            should_publish_public=summary.should_publish_public,
            no_publish_reason=summary.no_publish_reason,
            summary=compact_summary,
        )

    def _build_admin_warning_message(
        self,
        *,
        symbol: str,
        date_us: date | None,
        date_kst: date | None,
        source_environment: str,
        reason: str,
        detail: str,
        room_name: str | None,
    ) -> str:
        lines = ["[IMVT 옵션 심리 경고]"]
        if room_name:
            lines.append(f"- 방: {room_name}")
        if date_us is not None or date_kst is not None:
            us_label = date_us.isoformat() if date_us is not None else "-"
            kst_label = date_kst.isoformat() if date_kst is not None else "-"
            lines.append(f"- 기준일: US {us_label} / KST {kst_label}")
        lines.extend(
            [
                f"- 심볼: {symbol}",
                f"- 환경: {source_environment}",
                f"- 사유: {reason}",
                f"- 상세: {detail}",
            ]
        )
        return "\n".join(lines)

    def _describe_warning_reason(self, reason: str, *, public_message_included: bool = False) -> str:
        if public_message_included:
            descriptions = {
                self.NO_PUBLISH_SANDBOX: "TRADIER_ENV=sandbox라 공개방 옵션 요약은 생략했습니다.",
                self.NO_PUBLISH_PARTIAL_CHAIN: "일부 옵션 체인 수집이 실패해 수치 해석에 주의가 필요합니다.",
                self.NO_PUBLISH_LOW_OI: "옵션 미결제약정이 적어 수치 해석에 주의가 필요합니다.",
                self.NO_PUBLISH_SINGLE_EXPIRY_DISTORTION: "단일 만기 비중이 과도해 수치 해석에 주의가 필요합니다.",
                self.NO_PUBLISH_CALL_OI_ZERO: "콜 미결제약정이 0이라 PCR 해석 신뢰도가 낮습니다.",
                self.NO_PUBLISH_CALL_VOL_ZERO: "콜 거래량이 0이라 PCR 해석 신뢰도가 낮습니다.",
                self.NO_PUBLISH_PCR_UNAVAILABLE: "PCR 계산에 필요한 데이터가 부족합니다.",
                self.NO_PUBLISH_MISSING_REQUIRED_PUBLIC_FIELDS: "일부 표시 필수 데이터가 누락돼 관리자 경고를 남깁니다.",
                self.WARNING_COLLECTION_FAILED: "옵션 심리 수집이 실패했습니다.",
                self.WARNING_PUBLIC_DELIVERY_FAILED: "공개방 옵션 메시지 전송이 실패했습니다.",
            }
            return descriptions.get(reason, reason)

        descriptions = {
            self.NO_PUBLISH_SANDBOX: "TRADIER_ENV=sandbox라 공개방 발행을 차단했습니다.",
            self.NO_PUBLISH_PARTIAL_CHAIN: "일부 옵션 체인 수집이 실패해 공개방 발행을 차단했습니다.",
            self.NO_PUBLISH_LOW_OI: "옵션 미결제약정이 너무 적어 공개방 발행을 차단했습니다.",
            self.NO_PUBLISH_SINGLE_EXPIRY_DISTORTION: "단일 만기 비중이 과도해 공개방 발행을 차단했습니다.",
            self.NO_PUBLISH_CALL_OI_ZERO: "콜 미결제약정이 0이라 PCR 계산 기준이 불충분합니다.",
            self.NO_PUBLISH_CALL_VOL_ZERO: "콜 거래량이 0이라 PCR 계산 기준이 불충분합니다.",
            self.NO_PUBLISH_PCR_UNAVAILABLE: "PCR 계산에 필요한 데이터가 부족합니다.",
            self.NO_PUBLISH_MISSING_REQUIRED_PUBLIC_FIELDS: "공개방 발행 필수 데이터가 누락되었습니다.",
            self.WARNING_COLLECTION_FAILED: "옵션 심리 수집이 실패했습니다.",
            self.WARNING_PUBLIC_DELIVERY_FAILED: "공개방 옵션 메시지 전송이 실패했습니다.",
        }
        return descriptions.get(reason, reason)

    def _select_quality_flag(self, quality_flags: set[str]) -> str:
        for flag in self.QUALITY_PRIORITY:
            if flag in quality_flags:
                return flag
        return self.QUALITY_OK

    @staticmethod
    def _safe_ratio(numerator: int, denominator: int) -> float | None:
        if denominator <= 0:
            return None
        return round(numerator / denominator, 4)

    @staticmethod
    def _safe_delta(current_value: float | None, previous_value: float | None) -> float | None:
        if current_value is None or previous_value is None:
            return None
        return round(current_value - previous_value, 4)

    @staticmethod
    def _to_int(value: Any) -> int:
        if value in (None, ""):
            return 0
        return int(float(value))

    @staticmethod
    def _to_float(value: Any) -> float | None:
        if value in (None, ""):
            return None
        return float(value)

    def _extract_change_pct(self, quote: dict[str, Any], close: float | None) -> float | None:
        direct = self._to_float(quote.get("change_percentage"))
        if direct is not None:
            return direct
        prev_close = self._to_float(quote.get("prevclose") or quote.get("previous_close"))
        if close is None or prev_close in (None, 0):
            return None
        return round(((close - prev_close) / prev_close) * 100, 4)

    @staticmethod
    def _has_single_expiry_distortion(by_expiry_rows: list[dict[str, Any]], totals: dict[str, Any]) -> bool:
        total_option_oi = int(totals["total_option_oi"])
        total_option_volume = int(totals["total_option_volume"])
        for row in by_expiry_rows:
            expiry_oi = int(row["total_option_oi"])
            expiry_volume = int(row["total_option_volume"])
            if total_option_oi > 0 and expiry_oi / total_option_oi >= 0.7:
                return True
            if total_option_volume > 0 and expiry_volume / total_option_volume >= 0.7:
                return True
        return False

    @staticmethod
    def _format_ratio(value: float | None) -> str:
        if value is None:
            return "N/A"
        return f"{value:.2f}"

    def _build_quote_line(self, summary: OptionsPCRDailySummary) -> str | None:
        if summary.close is None and summary.change_1d_pct is None:
            return None
        if summary.close is None:
            return f"- 전일 대비: {self._format_signed_pct(summary.change_1d_pct)}"
        if summary.change_1d_pct is None:
            return f"- 종가: ${summary.close:.2f}"
        return f"- 종가: ${summary.close:.2f} ({self._format_signed_pct(summary.change_1d_pct)})"

    @staticmethod
    def _format_signed_pct(value: float | None) -> str:
        if value is None:
            return "N/A"
        return f"{value:+.2f}%"

    @staticmethod
    def _describe_ratio(value: float | None, *, default: str) -> str:
        if value is None:
            return default
        if value > 1.05:
            return f"풋 우위입니다. PCR {value:.2f}"
        if value < 0.95:
            return f"콜 우위입니다. PCR {value:.2f}"
        return f"대체로 중립권입니다. PCR {value:.2f}"
