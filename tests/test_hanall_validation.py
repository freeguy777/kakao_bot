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
