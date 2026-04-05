from __future__ import annotations

import copy
import os
import re
import unittest
from collections import Counter

import requests

from server.config import get_hanall_sources_config
from server.core.hanall_news_models import OfficialCollectionResult
from server.infra.hanall_news_collectors import COLLECTOR_CLASSES
from server.settings import get_settings


def _enabled_mfds_services() -> list[str]:
    raw = os.getenv("HANALL_LIVE_MFDS_SERVICES", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


@unittest.skipUnless(os.getenv("RUN_HANALL_LIVE_SMOKE") == "1", "set RUN_HANALL_LIVE_SMOKE=1")
class HanallLiveCollectorSmokeTest(unittest.TestCase):
    def _run_source(self, source_key: str) -> None:
        config = get_hanall_sources_config()
        collector_config = copy.deepcopy(config.get("collectors", {}).get(source_key, {}))
        settings = get_settings()

        if not collector_config:
            self.fail(f"missing collector config for {source_key}")

        if source_key == "mfds":
            services = _enabled_mfds_services()
            if not services:
                raise unittest.SkipTest("set HANALL_LIVE_MFDS_SERVICES for approval-gated MFDS datasets")
            collector_config["enabled"] = True
            collector_config.setdefault("services", {})
            for service_name in list(collector_config["services"].keys()):
                collector_config["services"][service_name] = service_name in services

        collector = COLLECTOR_CLASSES[source_key](settings, collector_config)
        if collector._requires_api_key() and not collector._api_key():
            raise unittest.SkipTest(f"missing API key for {source_key}")

        with requests.Session() as session:
            result = collector.collect(session)

        self.assertIsInstance(result, OfficialCollectionResult)
        self.assertGreaterEqual(len(result.checked_source_log) + len(result.coverage_gaps), 1)
        self.assertIsInstance(result.findings, list)
        status_summary = ",".join(
            f"{status}={count}" for status, count in Counter(entry.status for entry in result.checked_source_log).most_common()
        ) or "-"
        gap_summary = ",".join(
            f"{gap_type}={count}" for gap_type, count in Counter(gap.gap_type for gap in result.coverage_gaps).most_common()
        ) or "-"
        auth_mode_counter = Counter()
        for entry in result.checked_source_log:
            match = re.search(r"auth_mode=([a-z_]+)", entry.note or "")
            if match:
                auth_mode_counter[match.group(1)] += 1
        auth_mode_summary = ",".join(f"{mode}={count}" for mode, count in auth_mode_counter.most_common()) or "-"
        print(
            f"SMOKE source={source_key} findings={len(result.findings)} statuses={status_summary} auth_modes={auth_mode_summary} gaps={gap_summary}"
        )

    def test_fmp(self) -> None:
        self._run_source("fmp")

    def test_sec_official(self) -> None:
        self._run_source("sec_official")

    def test_opendart(self) -> None:
        self._run_source("opendart")

    def test_openfda(self) -> None:
        self._run_source("openfda")

    def test_clinicaltrials(self) -> None:
        self._run_source("clinicaltrials")

    def test_cris(self) -> None:
        self._run_source("cris")

    def test_mfds(self) -> None:
        self._run_source("mfds")

    def test_ncbi(self) -> None:
        self._run_source("ncbi")

    def test_europe_pmc(self) -> None:
        self._run_source("europe_pmc")

    def test_crossref(self) -> None:
        self._run_source("crossref")

    def test_biorxiv_and_medrxiv(self) -> None:
        self._run_source("biorxiv")
