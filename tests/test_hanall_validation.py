from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from app.schemas import HanallApiBundle, HanallRenderedOutput, HanallSourceStatus, HanallStructuredFact
from app.services.hanall_research_service import HanallResearchService


INVALID_PUBLIC = """📅 2026-04-14 07:42 KST 기준

1. 🏢 한올/IMVT 직접 업데이트 : 0건
- 직전 공식 업데이트 이후 24시간 내 신규 공시/규제 문서/임상등록 업데이트 없음

2. 🧬 경쟁사/파이프라인 체크 : 0건
- 24시간 내 경쟁사 공식 업데이트 없음

3. 🔎 이번에 확인한 범위 : 2개 범주
- DART, ClinicalTrials.gov

4. 👀 참고할 포인트 : 1개
- 공식 소스 점검 지속"""

INVALID_ADMIN = """A. 요약
- 지난 24시간 내 Confirmed 업데이트 총수: 0건
- 한올/Immunovant 직접 업데이트: 0건
- 경쟁사 중요 업데이트: 0건
- 가장 중요한 5개 이슈: 없음
- 전반적 커버리지 수준: Medium

B. Confirmed Updates — Company Direct
| KST 시각 | 엔터티 | 분류 | 정확한 사실 | 핵심 숫자/날짜 | 1차 출처 | 보조 출처 | 코멘트(추측 금지) |
|---|---|---|---|---|---|---|---|
| 없음 | - | - | 24시간 내 신규 사실 미확인 | - | - | - | - |

C. Confirmed Updates — Competitor Relevant
| KST 시각 | 경쟁사 | 자산 | 분류(Direct class / Indication / Standard-of-care / Regional) | 관련 한올/IMVT 자산 | 적응증 | 정확한 사실 | 핵심 숫자/날짜 | 1차 출처 | 왜 중요한지(팩트 기반 1문장) |
|---|---|---|---|---|---|---|---|---|---|
| 없음 | - | - | - | - | - | 24시간 내 high/medium impact 경쟁사 업데이트 미확인 | - | - | - |

D. Competitor Map Snapshot
| 경쟁사 | 자산 | target/MoA | 적응증 | 단계/승인상태 | 지역 | universe 포함 근거 출처 |
|---|---|---|---|---|---|---|
| Argenx | efgartigimod | FcRn inhibitor | gMG | 승인 | 글로벌 | FDA Label |

E. Checked Source Log
| 소스 | 상태(새 항목 있음 / 확인했으나 신규 없음 / 접근 제한) | 마지막 확인 시각(KST) | 비고 |
|---|---|---|---|
| OpenDART | 확인했으나 신규 없음 | 2026-04-14 07:42 | 목록 점검 |

F. Unverified Leads
- 없음

G. Coverage Gaps
- 없음

H. Omission Audit
- 공식 회사/IR 확인 완료 여부: 완료
- 규제/거래소/공시 확인 완료 여부: 완료
"""

CORRECTED_PUBLIC = """📅 2026-04-14 07:42 KST 기준

1. 🏢 한올/IMVT 직접 업데이트 : 1건
- 한올바이오파마가 2026-04-13 15:54 KST 기업설명회(IR)개최(안내공시)를 제출했고 2026-04-14 09:00 투자자 대상 주요 경영현황 설명 및 질의응답 일정을 공지함

2. 🧬 경쟁사/파이프라인 체크 : 0건
- 24시간 내 경쟁사 공식 업데이트 없음

3. 🔎 이번에 확인한 범위 : 2개 범주
- DART, ClinicalTrials.gov

4. 👀 참고할 포인트 : 1개
- 공식 소스 점검 지속"""

CORRECTED_ADMIN = """A. 요약
- 지난 24시간 내 Confirmed 업데이트 총수: 1건
- 한올/Immunovant 직접 업데이트: 1건
- 경쟁사 중요 업데이트: 0건
- 가장 중요한 5개 이슈: 한올바이오파마 기업설명회(IR)개최(안내공시)
- 전반적 커버리지 수준: High

B. Confirmed Updates — Company Direct
| KST 시각 | 엔터티 | 분류 | 정확한 사실 | 핵심 숫자/날짜 | 1차 출처 | 보조 출처 | 코멘트(추측 금지) |
|---|---|---|---|---|---|---|---|
| 2026-04-13 15:54 KST | HanAll Biopharma | SEC/DART/KRX | 기업설명회(IR)개최(안내공시) 접수 | rcpNo 20260413800541, 개최일자 2026-04-14, 개최시각 09:00 | OpenDART | 없음 | 주요 사업 현황 설명 및 질의응답 예정 |

C. Confirmed Updates — Competitor Relevant
| KST 시각 | 경쟁사 | 자산 | 분류(Direct class / Indication / Standard-of-care / Regional) | 관련 한올/IMVT 자산 | 적응증 | 정확한 사실 | 핵심 숫자/날짜 | 1차 출처 | 왜 중요한지(팩트 기반 1문장) |
|---|---|---|---|---|---|---|---|---|---|
| 없음 | - | - | - | - | - | 24시간 내 high/medium impact 경쟁사 업데이트 미확인 | - | - | - |

D. Competitor Map Snapshot
| 경쟁사 | 자산 | target/MoA | 적응증 | 단계/승인상태 | 지역 | universe 포함 근거 출처 |
|---|---|---|---|---|---|---|
| Argenx | efgartigimod | FcRn inhibitor | gMG | 승인 | 글로벌 | FDA Label |

E. Checked Source Log
| 소스 | 상태(새 항목 있음 / 확인했으나 신규 없음 / 접근 제한) | 마지막 확인 시각(KST) | 비고 |
|---|---|---|---|
| OpenDART | 새 항목 있음 | 2026-04-14 07:42 | 기업설명회(IR)개최(안내공시) 확인 |

F. Unverified Leads
- 없음

G. Coverage Gaps
- 없음

H. Omission Audit
- 공식 회사/IR 확인 완료 여부: 완료
- 규제/거래소/공시 확인 완료 여부: 완료
"""


class FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_calls = []
        self.reasoning_content = None

    def model_dump(self, mode: str = "json", exclude_none: bool = True):
        return {"role": "assistant", "content": self.content}


class FakeChoice:
    def __init__(self, message: FakeMessage) -> None:
        self.finish_reason = "stop"
        self.message = message


class FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [FakeChoice(FakeMessage(content))]

    def model_dump(self, mode: str = "json"):
        return {"choices": [{"finish_reason": "stop"}]}


class SequencedCompletions:
    def __init__(self, contents: list[str]) -> None:
        self._contents = list(contents)
        self.received_messages: list[list[dict[str, str]]] = []

    async def create(self, **kwargs: object):
        self.received_messages.append(kwargs["messages"])
        return FakeResponse(self._contents.pop(0))


class SequencedClient:
    def __init__(self, contents: list[str]) -> None:
        self.chat = type("Chat", (), {"completions": SequencedCompletions(contents)})()


class FakePrefetchService:
    def __init__(self, bundle: HanallApiBundle) -> None:
        self._bundle = bundle

    async def collect(self, *, window_start: datetime, window_end: datetime) -> HanallApiBundle:
        return self._bundle


def _build_service(test_settings, tmp_path: Path, *, bundle: HanallApiBundle, contents: list[str]) -> HanallResearchService:
    spec_path = tmp_path / "hanall_spec.md"
    spec_path.write_text("한올 business spec 원문", encoding="utf-8")
    test_settings.hanall_spec_path = spec_path
    service = HanallResearchService(
        settings=test_settings,
        prompts=test_settings.load_prompts(),
        artifact_repository=type("Repo", (), {"get_by_key": lambda *_: None, "save": lambda *args, **kwargs: args[1]})(),
        hanall_prefetch_service=FakePrefetchService(bundle),
    )
    service._client = SequencedClient(contents)
    service._load_formula_tools = lambda: []
    return service


def test_validate_rendered_output_flags_no_update_claim_when_hard_source_is_unavailable(test_settings, tmp_path: Path) -> None:
    bundle = HanallApiBundle(
        source_statuses=[
            HanallSourceStatus(
                source_name="OpenDART",
                status="unavailable",
                checked_at=datetime(2026, 4, 14, 7, 42),
                detail="timeout",
                hard_requirement=True,
            )
        ]
    )
    service = _build_service(
        test_settings,
        tmp_path,
        bundle=bundle,
        contents=[f"<public_brief>{INVALID_PUBLIC}</public_brief>\n<admin_report>{INVALID_ADMIN}</admin_report>"],
    )

    validation = service._validate_rendered_output(
        HanallRenderedOutput(public_text=INVALID_PUBLIC, admin_text=INVALID_ADMIN),
        bundle,
    )

    assert validation.is_valid is False
    assert "unavailable" in validation.issues[0]


def test_validate_rendered_output_requires_soft_direct_fact_in_admin_and_public(test_settings, tmp_path: Path) -> None:
    bundle = HanallApiBundle(
        facts=[
            HanallStructuredFact(
                source_name="ClinicalTrials.gov API",
                source_type="clinical_registry",
                entity="HanAll/Immunovant",
                category="임상",
                title="A Study to Assess the Efficacy, Safety, and Tolerability of IMVT-1402 as Treatment for Adult Participants With Graves' Disease",
                fact_text="... NCT06727604 ...",
                source_id="NCT06727604",
                source_url="https://clinicaltrials.gov/study/NCT06727604",
                observed_date=date(2026, 4, 13),
                validation_mode="soft",
            )
        ],
        source_statuses=[
            HanallSourceStatus(
                source_name="ClinicalTrials.gov API",
                status="ok",
                checked_at=datetime(2026, 4, 14, 7, 42),
                detail="임상등록 업데이트 1건 감지",
                hard_requirement=True,
            )
        ],
    )
    service = _build_service(
        test_settings,
        tmp_path,
        bundle=bundle,
        contents=[f"<public_brief>{INVALID_PUBLIC}</public_brief>\n<admin_report>{INVALID_ADMIN}</admin_report>"],
    )

    validation = service._validate_rendered_output(
        HanallRenderedOutput(public_text=INVALID_PUBLIC, admin_text=INVALID_ADMIN),
        bundle,
    )

    assert validation.is_valid is False
    assert "structured direct fact 누락(admin): NCT06727604" in validation.issues
    assert "structured direct fact 누락(public): NCT06727604" in validation.issues


def test_validate_rendered_output_requires_kind_krx_fact_in_admin_and_public(test_settings, tmp_path: Path) -> None:
    bundle = HanallApiBundle(
        facts=[
            HanallStructuredFact(
                source_name="KIND/KRX",
                source_type="filing",
                entity="HanAll Biopharma",
                category="SEC/DART/KRX",
                title="[투자주의]투자경고종목 지정예고",
                fact_text=(
                    "[투자주의]투자경고종목 지정예고 KIND/KRX 공시가 2026-05-27 20:00 KST에 확인됨, "
                    "지정/예고일: 2026년 05월 28일"
                ),
                source_id="20260527000934",
                source_url="https://kind.krx.co.kr/external/2026/05/27/000934/20260527002162/68807.htm",
                observed_at=datetime(2026, 5, 27, 20, 0),
                validation_mode="hard",
            )
        ],
        source_statuses=[
            HanallSourceStatus(
                source_name="KIND/KRX",
                status="ok",
                checked_at=datetime(2026, 5, 28, 7, 42),
                detail="KIND/KRX 1건 감지",
                hard_requirement=True,
            )
        ],
    )
    service = _build_service(
        test_settings,
        tmp_path,
        bundle=bundle,
        contents=[f"<public_brief>{INVALID_PUBLIC}</public_brief>\n<admin_report>{INVALID_ADMIN}</admin_report>"],
    )

    validation = service._validate_rendered_output(
        HanallRenderedOutput(public_text=INVALID_PUBLIC, admin_text=INVALID_ADMIN),
        bundle,
    )

    assert validation.is_valid is False
    assert "structured direct fact 누락(admin): 20260527000934" in validation.issues
    assert "structured direct fact 누락(public): 20260527000934" in validation.issues


def test_validate_rendered_output_flags_kind_krx_claim_when_source_was_not_checked(test_settings, tmp_path: Path) -> None:
    public_text = """📅 2026-05-28 07:42 KST 기준

1. 🏢 한올/IMVT 직접 업데이트 : 0건
- KIND/KRX 시장경보 신규 없음 확인.

2. 🧬 경쟁사/파이프라인 체크 : 0건
- 24시간 내 경쟁사 공식 업데이트 없음

3. 🔎 이번에 확인한 범위 : 2개 범주
- OpenDART, SEC EDGAR

4. 👀 참고할 포인트 : 1개
- 공식 소스 점검 지속"""
    admin_text = INVALID_ADMIN.replace("OpenDART | 확인했으나 신규 없음", "KIND/KRX | 확인했으나 신규 없음")
    bundle = HanallApiBundle(
        source_statuses=[
            HanallSourceStatus(
                source_name="OpenDART",
                status="ok",
                checked_at=datetime(2026, 5, 28, 7, 42),
                detail="공시 없음",
                hard_requirement=True,
            )
        ]
    )
    service = _build_service(
        test_settings,
        tmp_path,
        bundle=bundle,
        contents=[f"<public_brief>{public_text}</public_brief>\n<admin_report>{admin_text}</admin_report>"],
    )

    validation = service._validate_rendered_output(
        HanallRenderedOutput(public_text=public_text, admin_text=admin_text),
        bundle,
    )

    assert validation.is_valid is False
    assert any("KIND/KRX source" in issue for issue in validation.issues)


async def test_hanall_collect_repairs_missing_hard_fact_with_prefetch_bundle(test_settings, tmp_path: Path) -> None:
    bundle = HanallApiBundle(
        facts=[
            HanallStructuredFact(
                source_name="OpenDART",
                source_type="filing",
                entity="HanAll Biopharma",
                category="SEC/DART/KRX",
                title="기업설명회(IR)개최(안내공시)",
                fact_text="기업설명회(IR)개최(안내공시) 공시가 2026-04-13 15:54 KST 기준 OpenDART에서 확인됨",
                source_id="20260413800541",
                source_url="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260413800541",
                observed_at=datetime(2026, 4, 13, 15, 54),
                validation_mode="hard",
            )
        ],
        source_statuses=[
            HanallSourceStatus(
                source_name="OpenDART",
                status="ok",
                checked_at=datetime(2026, 4, 14, 7, 42),
                detail="공시 1건 감지",
                hard_requirement=True,
            )
        ],
    )
    invalid_text = f"<public_brief>{INVALID_PUBLIC}</public_brief>\n<admin_report>{INVALID_ADMIN}</admin_report>"
    corrected_text = f"<public_brief>{CORRECTED_PUBLIC}</public_brief>\n<admin_report>{CORRECTED_ADMIN}</admin_report>"
    service = _build_service(test_settings, tmp_path, bundle=bundle, contents=[invalid_text, corrected_text])

    artifact = await service.get_or_create_daily_artifact(date(2026, 4, 14))

    assert artifact.summary_text == CORRECTED_PUBLIC
    assert "기업설명회(IR)개최(안내공시)" in artifact.detail_text
    assert artifact.raw_response["validation"]["repair_attempted"] is True
    first_prompt = service._client.chat.completions.received_messages[0][1]["content"]
    assert "[Structured API facts]" in first_prompt
    assert "20260413800541" in first_prompt


async def test_hanall_collect_format_repairs_validation_repair_output(test_settings, tmp_path: Path) -> None:
    bundle = HanallApiBundle(
        facts=[
            HanallStructuredFact(
                source_name="OpenDART",
                source_type="filing",
                entity="HanAll Biopharma",
                category="SEC/DART/KRX",
                title="기업설명회(IR)개최(안내공시)",
                fact_text="기업설명회(IR)개최(안내공시) 공시가 2026-04-13 15:54 KST 기준 OpenDART에서 확인됨",
                source_id="20260413800541",
                source_url="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260413800541",
                observed_at=datetime(2026, 4, 13, 15, 54),
                validation_mode="hard",
            )
        ],
        source_statuses=[
            HanallSourceStatus(
                source_name="OpenDART",
                status="ok",
                checked_at=datetime(2026, 4, 14, 7, 42),
                detail="공시 1건 감지",
                hard_requirement=True,
            )
        ],
    )
    invalid_text = f"<public_brief>{INVALID_PUBLIC}</public_brief>\n<admin_report>{INVALID_ADMIN}</admin_report>"
    corrected_text = f"<public_brief>{CORRECTED_PUBLIC}</public_brief>\n<admin_report>{CORRECTED_ADMIN}</admin_report>"
    service = _build_service(test_settings, tmp_path, bundle=bundle, contents=[invalid_text, CORRECTED_ADMIN, corrected_text])

    artifact = await service.get_or_create_daily_artifact(date(2026, 4, 14))

    assert artifact.summary_text == CORRECTED_PUBLIC
    assert artifact.raw_response["parse"]["format_repair_attempted"] is True
    assert artifact.raw_response["validation"]["repair_attempted"] is True
    received_messages = service._client.chat.completions.received_messages
    assert len(received_messages) == 3
    assert "저장 전 검증에 실패" in received_messages[1][-1]["content"]
    assert "저장 전 출력 태그 검증에 실패" in received_messages[2][-1]["content"]


async def test_hanall_collect_repairs_missing_soft_direct_fact_with_prefetch_bundle(test_settings, tmp_path: Path) -> None:
    bundle = HanallApiBundle(
        facts=[
            HanallStructuredFact(
                source_name="ClinicalTrials.gov API",
                source_type="clinical_registry",
                entity="HanAll/Immunovant",
                category="임상",
                title="A Study to Assess the Efficacy, Safety, and Tolerability of IMVT-1402 as Treatment for Adult Participants With Graves' Disease",
                fact_text="A Study ... NCT06727604 ...",
                source_id="NCT06727604",
                source_url="https://clinicaltrials.gov/study/NCT06727604",
                observed_date=date(2026, 4, 13),
                validation_mode="soft",
            )
        ],
        source_statuses=[
            HanallSourceStatus(
                source_name="ClinicalTrials.gov API",
                status="ok",
                checked_at=datetime(2026, 4, 14, 7, 42),
                detail="임상등록 업데이트 1건 감지",
                hard_requirement=True,
            )
        ],
    )
    corrected_public = """📅 2026-04-14 07:42 KST 기준

1. 🏢 한올/IMVT 직접 업데이트 : 1건
- IMVT-1402 Graves' Disease 임상등록 업데이트가 확인됐고 ClinicalTrials.gov NCT06727604 레코드의 2026-04-13 업데이트가 반영됨

2. 🧬 경쟁사/파이프라인 체크 : 0건
- 24시간 내 경쟁사 공식 업데이트 없음

3. 🔎 이번에 확인한 범위 : 2개 범주
- DART, ClinicalTrials.gov

4. 👀 참고할 포인트 : 1개
- 공식 소스 점검 지속"""
    corrected_admin = """A. 요약
- 지난 24시간 내 Confirmed 업데이트 총수: 1건
- 한올/Immunovant 직접 업데이트: 1건
- 경쟁사 중요 업데이트: 0건
- 가장 중요한 5개 이슈: IMVT-1402 Graves' Disease 임상등록 업데이트
- 전반적 커버리지 수준: High

B. Confirmed Updates — Company Direct
| KST 시각 | 엔터티 | 분류 | 정확한 사실 | 핵심 숫자/날짜 | 1차 출처 | 보조 출처 | 코멘트(추측 금지) |
|---|---|---|---|---|---|---|---|
| 2026-04-13 | Immunovant/HanAll | 임상 | IMVT-1402 Graves' Disease 레코드 업데이트 | NCT06727604, Last Update Posted 2026-04-13 | ClinicalTrials.gov API | 없음 | 모집 상태 유지 |

C. Confirmed Updates — Competitor Relevant
| KST 시각 | 경쟁사 | 자산 | 분류(Direct class / Indication / Standard-of-care / Regional) | 관련 한올/IMVT 자산 | 적응증 | 정확한 사실 | 핵심 숫자/날짜 | 1차 출처 | 왜 중요한지(팩트 기반 1문장) |
|---|---|---|---|---|---|---|---|---|---|
| 없음 | - | - | - | - | - | 24시간 내 high/medium impact 경쟁사 업데이트 미확인 | - | - | - |

D. Competitor Map Snapshot
| 경쟁사 | 자산 | target/MoA | 적응증 | 단계/승인상태 | 지역 | universe 포함 근거 출처 |
|---|---|---|---|---|---|---|
| Argenx | efgartigimod | FcRn inhibitor | gMG | 승인 | 글로벌 | FDA Label |

E. Checked Source Log
| 소스 | 상태(새 항목 있음 / 확인했으나 신규 없음 / 접근 제한) | 마지막 확인 시각(KST) | 비고 |
|---|---|---|---|
| ClinicalTrials.gov API | 새 항목 있음 | 2026-04-14 07:42 | NCT06727604 확인 |

F. Unverified Leads
- 없음

G. Coverage Gaps
- 없음

H. Omission Audit
- 공식 회사/IR 확인 완료 여부: 완료
- 규제/거래소/공시 확인 완료 여부: 완료
"""
    invalid_text = f"<public_brief>{INVALID_PUBLIC}</public_brief>\n<admin_report>{INVALID_ADMIN}</admin_report>"
    corrected_text = f"<public_brief>{corrected_public}</public_brief>\n<admin_report>{corrected_admin}</admin_report>"
    service = _build_service(test_settings, tmp_path, bundle=bundle, contents=[invalid_text, corrected_text])

    artifact = await service.get_or_create_daily_artifact(date(2026, 4, 14))

    assert artifact.summary_text == corrected_public
    assert "NCT06727604" in artifact.detail_text
    assert artifact.raw_response["validation"]["repair_attempted"] is True


def test_validate_rendered_output_requires_deep_research_detail_tokens(test_settings, tmp_path: Path) -> None:
    bundle = HanallApiBundle(
        facts=[
            HanallStructuredFact(
                source_name="SEC EDGAR API",
                source_type="filing",
                entity="Immunovant",
                category="SEC/DART/KRX",
                title="8-K | Current report",
                fact_text="Immunovant 8-K filing이 2026-05-20 16:14 KST 기준 SEC EDGAR submissions에서 확인됨",
                source_id="0001764013-26-000062",
                source_url="https://www.sec.gov/Archives/edgar/data/1764013/000176401326000062/imvt-20260520.htm",
                observed_at=datetime(2026, 5, 20, 16, 14),
                validation_mode="hard",
            )
        ]
    )
    public_text = """📅 2026-05-21 07:42 KST 기준

1. 🏢 한올/IMVT 직접 업데이트 : 1건
- Immunovant FY2026 4Q 실적 및 사업 업데이트(Form 8-K, id=0001764013-26-000062) 확인

2. 🧬 경쟁사/파이프라인 체크 : 0건
- FcRn 경쟁축 공식 업데이트 없음

3. 🔎 이번에 확인한 범위 : 2개 범주
- SEC EDGAR, Immunovant IR

4. 👀 참고할 포인트 : 1개
- 공식 원문 확인 필요"""
    admin_text = """A. 요약
- 직접 업데이트 1건

B. Confirmed Updates — Company Direct
| KST 시각 | 엔터티 | 분류 | 정확한 사실 | 핵심 숫자/날짜 | 1차 출처 | 보조 출처 | 코멘트(추측 금지) |
| 2026-05-20 16:14 KST | Immunovant | SEC/DART/KRX | 8-K Current report 제출 | id=0001764013-26-000062 | SEC EDGAR | 없음 | filing metadata 확인 |

C. Confirmed Updates — Competitor Relevant
| KST 시각 | 경쟁사 | 자산 | 분류(Direct class / Indication / Standard-of-care / Regional) | 관련 한올/IMVT 자산 | 적응증 | 정확한 사실 | 핵심 숫자/날짜 | 1차 출처 | 왜 중요한지(팩트 기반 1문장) |
| 없음 | - | - | - | - | - | 없음 | - | - | - |

D. Competitor Map Snapshot
| 경쟁사 | 자산 | target/MoA | 적응증 | 단계/승인상태 | 지역 | universe 포함 근거 출처 |
| Argenx | efgartigimod | FcRn inhibitor | gMG | 승인 | 글로벌 | FDA Label |

E. Checked Source Log
| 소스 | 상태(새 항목 있음 / 확인했으나 신규 없음 / 접근 제한) | 마지막 확인 시각(KST) | 비고 |
| SEC EDGAR | 새 항목 있음 | 2026-05-21 07:42 | 8-K 확인 |

F. Unverified Leads
- 없음

G. Coverage Gaps
- 없음

H. Omission Audit
- 공식 회사/IR 확인 완료 여부: 완료
- 규제/거래소/공시 확인 완료 여부: 완료"""
    deep_research_text = (
        "status: success\n"
        "confirmed direct-event details: Immunovant는 FY2026 4Q 업데이트에서 IMVT-1402 D2T RA Week 16 "
        "ACR20/50/70 72.7%/54.5%/35.8%와 현금 약 $902.1M을 공시함\n"
        "public brief required points: IMVT-1402 D2T RA Week 16 ACR20/50/70 72.7%/54.5%/35.8%"
    )
    service = _build_service(
        test_settings,
        tmp_path,
        bundle=bundle,
        contents=[],
    )

    validation = service._validate_rendered_output(
        HanallRenderedOutput(public_text=public_text, admin_text=admin_text),
        bundle,
        deep_research_text=deep_research_text,
    )

    assert validation.is_valid is False
    assert any(issue.startswith("deep research 핵심 내용 누락(public)") for issue in validation.issues)
    assert any(issue.startswith("deep research 핵심 내용 누락(admin)") for issue in validation.issues)


def test_deep_research_quality_flags_d2t_ra_topline_without_acr_values(test_settings, tmp_path: Path) -> None:
    bundle = HanallApiBundle(
        facts=[
            HanallStructuredFact(
                source_name="SEC EDGAR API",
                source_type="filing",
                entity="Immunovant",
                category="SEC/DART/KRX",
                title="8-K | 8-K",
                fact_text="Immunovant 8-K filing이 2026-05-20 20:14 KST 기준 SEC EDGAR submissions에서 확인됨 (8-K)",
                source_id="0001764013-26-000062",
                source_url="https://www.sec.gov/Archives/edgar/data/1764013/000176401326000062/imvt-20260520.htm",
                observed_at=datetime(2026, 5, 20, 20, 14),
                validation_mode="hard",
            )
        ]
    )
    service = _build_service(test_settings, tmp_path, bundle=bundle, contents=[])

    issues = service._deep_research_quality_issues(
        bundle.facts,
        (
            "status: partial\n"
            "confirmed direct-event details: Immunovant 8-K Item 2.02 confirmed. "
            "IMVT-1402 D2T RA 2026년 topline 결과 예고, Graves' Disease 2027년 topline 결과 예고."
        ),
    )

    assert "Exhibit 99.1/official Press Release" in issues[0]
    assert any("D2T RA" in issue and "ACR20/50/70" in issue for issue in issues)


async def test_hanall_collect_runs_deep_research_for_sec_business_update(test_settings, tmp_path: Path) -> None:
    accession_id = "0001764013-26-000062"
    bundle = HanallApiBundle(
        facts=[
            HanallStructuredFact(
                source_name="SEC EDGAR API",
                source_type="filing",
                entity="Immunovant",
                category="SEC/DART/KRX",
                title="8-K | Current report",
                fact_text="Immunovant 8-K filing이 2026-05-20 16:14 KST 기준 SEC EDGAR submissions에서 확인됨 (Current report)",
                source_id=accession_id,
                source_url="https://www.sec.gov/Archives/edgar/data/1764013/000176401326000062/imvt-20260520.htm",
                observed_at=datetime(2026, 5, 20, 16, 14),
                validation_mode="hard",
            )
        ],
        source_statuses=[
            HanallSourceStatus(
                source_name="SEC EDGAR API",
                status="ok",
                checked_at=datetime(2026, 5, 21, 7, 42),
                detail="SEC filing 1건 감지",
                hard_requirement=True,
            )
        ],
    )
    initial_public = f"""📅 2026-05-21 07:42 KST 기준

1. 🏢 한올/IMVT 직접 업데이트 : 1건
- Immunovant FY2026 4Q 실적 및 사업 업데이트(Form 8-K, id={accession_id}) 확인

2. 🧬 경쟁사/파이프라인 체크 : 0건
- FcRn 경쟁축 공식 업데이트 없음

3. 🔎 이번에 확인한 범위 : 2개 범주
- SEC EDGAR, Immunovant IR

4. 👀 참고할 포인트 : 1개
- 공식 원문 세부 확인 필요"""
    initial_admin = f"""A. 요약
- 직접 업데이트 1건

B. Confirmed Updates — Company Direct
| KST 시각 | 엔터티 | 분류 | 정확한 사실 | 핵심 숫자/날짜 | 1차 출처 | 보조 출처 | 코멘트(추측 금지) |
| 2026-05-20 16:14 KST | Immunovant | SEC/DART/KRX | 8-K Current report 제출 | id={accession_id} | SEC EDGAR | 없음 | filing metadata 확인 |

C. Confirmed Updates — Competitor Relevant
| KST 시각 | 경쟁사 | 자산 | 분류(Direct class / Indication / Standard-of-care / Regional) | 관련 한올/IMVT 자산 | 적응증 | 정확한 사실 | 핵심 숫자/날짜 | 1차 출처 | 왜 중요한지(팩트 기반 1문장) |
| 없음 | - | - | - | - | - | 없음 | - | - | - |

D. Competitor Map Snapshot
| 경쟁사 | 자산 | target/MoA | 적응증 | 단계/승인상태 | 지역 | universe 포함 근거 출처 |
| Argenx | efgartigimod | FcRn inhibitor | gMG | 승인 | 글로벌 | FDA Label |

E. Checked Source Log
| 소스 | 상태(새 항목 있음 / 확인했으나 신규 없음 / 접근 제한) | 마지막 확인 시각(KST) | 비고 |
| SEC EDGAR | 새 항목 있음 | 2026-05-21 07:42 | 8-K 확인 |

F. Unverified Leads
- 없음

G. Coverage Gaps
- 없음

H. Omission Audit
- 공식 회사/IR 확인 완료 여부: 완료
- 규제/거래소/공시 확인 완료 여부: 완료"""
    incomplete_deep_research_text = (
        "status: partial\n"
        "confirmed direct-event details: Immunovant 8-K Item 2.02 confirmed FY2026 results. "
        "IMVT-1402 D2T RA 2026년 topline 결과 예고, Graves' Disease 2027년 topline 결과 예고.\n"
        "public brief required points: IMVT-1402 D2T RA 2026년 topline 결과 예고\n"
        "coverage gaps: Exhibit 99.1 top highlights not checked"
    )
    repaired_deep_research_text = (
        "status: success\n"
        "confirmed direct-event details: Immunovant FY2026 4Q official Press Release / Exhibit 99.1 confirmed IMVT-1402 D2T RA "
        "Week 16 ACR20/50/70 72.7%/54.5%/35.8% and cash/cash equivalents of approximately $902.1M.\n"
        "public brief required points: Immunovant FY2026 4Q 업데이트는 IMVT-1402 D2T RA Week 16 "
        "ACR20/50/70 72.7%/54.5%/35.8%와 현금 약 $902.1M을 포함함\n"
        "coverage gaps: 없음"
    )
    final_public = f"""📅 2026-05-21 07:42 KST 기준

1. 🏢 한올/IMVT 직접 업데이트 : 1건
- Immunovant FY2026 4Q 업데이트(Form 8-K, id={accession_id}): IMVT-1402 D2T RA Week 16 ACR20/50/70 72.7%/54.5%/35.8%, 현금 약 $902.1M 제시

2. 🧬 경쟁사/파이프라인 체크 : 0건
- FcRn 경쟁축 공식 업데이트 없음

3. 🔎 이번에 확인한 범위 : 2개 범주
- SEC EDGAR, Immunovant IR

4. 👀 참고할 포인트 : 1개
- 실적발표 본문 핵심 수치를 2차 확인해 반영함"""
    final_admin = f"""A. 요약
- 직접 업데이트 1건

B. Confirmed Updates — Company Direct
| KST 시각 | 엔터티 | 분류 | 정확한 사실 | 핵심 숫자/날짜 | 1차 출처 | 보조 출처 | 코멘트(추측 금지) |
| 2026-05-20 16:14 KST | Immunovant | SEC/DART/KRX | FY2026 4Q business update에서 IMVT-1402 D2T RA Week 16 ACR20/50/70 결과와 현금 잔액을 공시 | id={accession_id}; ACR20/50/70 72.7%/54.5%/35.8%; cash 약 $902.1M | SEC EDGAR | Immunovant IR | 원문 기반 핵심 수치 |

C. Confirmed Updates — Competitor Relevant
| KST 시각 | 경쟁사 | 자산 | 분류(Direct class / Indication / Standard-of-care / Regional) | 관련 한올/IMVT 자산 | 적응증 | 정확한 사실 | 핵심 숫자/날짜 | 1차 출처 | 왜 중요한지(팩트 기반 1문장) |
| 없음 | - | - | - | - | - | 없음 | - | - | - |

D. Competitor Map Snapshot
| 경쟁사 | 자산 | target/MoA | 적응증 | 단계/승인상태 | 지역 | universe 포함 근거 출처 |
| Argenx | efgartigimod | FcRn inhibitor | gMG | 승인 | 글로벌 | FDA Label |

E. Checked Source Log
| 소스 | 상태(새 항목 있음 / 확인했으나 신규 없음 / 접근 제한) | 마지막 확인 시각(KST) | 비고 |
| SEC EDGAR | 새 항목 있음 | 2026-05-21 07:42 | 8-K 및 원문 확인 |

F. Unverified Leads
- 없음

G. Coverage Gaps
- 없음

H. Omission Audit
- 공식 회사/IR 확인 완료 여부: 완료
- 규제/거래소/공시 확인 완료 여부: 완료"""
    initial_text = f"<public_brief>{initial_public}</public_brief>\n<admin_report>{initial_admin}</admin_report>"
    final_text = f"<public_brief>{final_public}</public_brief>\n<admin_report>{final_admin}</admin_report>"
    service = _build_service(
        test_settings,
        tmp_path,
        bundle=bundle,
        contents=[initial_text, incomplete_deep_research_text, repaired_deep_research_text, final_text],
    )

    artifact = await service.get_or_create_daily_artifact(date(2026, 5, 21))

    assert "72.7%/54.5%/35.8%" in artifact.summary_text
    assert "$902.1M" in artifact.summary_text
    assert artifact.raw_response["deep_research"]["status"] == "success"
    assert artifact.raw_response["deep_research"]["quality_repair_attempted"] is True
    assert artifact.raw_response["deep_research"]["candidates"][0]["source_id"] == accession_id
    received_messages = service._client.chat.completions.received_messages
    assert any("2차 딥 리서치 단계" in message.get("content", "") for message in received_messages[1])
    assert any(accession_id in message.get("content", "") for message in received_messages[1])
    assert any("Previous deep research was incomplete" in message.get("content", "") for message in received_messages[2])
    assert any("ACR20/50/70" in message.get("content", "") for message in received_messages[2])
    assert "2차 딥 리서치 결과를 반영" in received_messages[3][-1]["content"]
    assert "IMVT-1402 D2T RA" in received_messages[3][-1]["content"]
