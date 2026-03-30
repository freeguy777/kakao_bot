# Kakao Bot Server

FastAPI 기반 카카오봇 서버다. 이번 기준으로 한올/Immunovant 24시간 브리핑은 단일 LLM 호출이 아니라 2단계 파이프라인으로 동작한다.

## HanAll 2-Stage Pipeline

외부 인터페이스는 유지한다.

- feature key: `hanall_news_brief`
- scheduler / service entrypoint: `build_hanall_news_brief()`
- API endpoint 경로: 기존 FastAPI 라우트 유지

내부 파이프라인은 다음 순서로 동작한다.

1. 공식 API collector 실행
2. raw finding 공통 스키마 정규화
3. stage1 Gemini 호출
4. RSS 보강 수집
5. stage2 Gemini 호출
6. 최종 카카오톡 텍스트 렌더 및 분할

## Stage Responsibilities

### Stage 1

- prompt key: `hanall_news_collect_prompt`
- 입력: 공식 API raw findings, source log, coverage gaps, 로컬 예정 이벤트, 관찰 범위
- 역할: dedupe, confirmed/unverified 분류, competitor snapshot, omission audit, search task 생성
- 출력: structured JSON
- Google Search tool 미사용

### Stage 2

- prompt key: `hanall_news_finalize_prompt`
- 입력: stage1 JSON, RSS 보강 결과, 로컬 예정 이벤트, 관찰 범위
- 역할: stage1 search task 우선 검색, 누락 보강, 최종 카카오톡 본문 작성
- 출력: 일반 텍스트
- `google_search` tool은 stage2에만 연결

## Source Separation

공식 API 1차 수집이 항상 우선이다.

- 공식 API: confirmed fact의 기준 소스
- RSS: 누락 후보와 참고 맥락 보강
- 검색: omission-fill 전용

검색 결과나 RSS hit는 공식 API로 이미 확인된 사실을 덮어쓰면 안 된다.

## Output Contract

최종 본문은 아래 섹션 순서를 유지한다.

1. `요약`
2. `오늘 예정 이벤트`
3. `Confirmed Updates — Company Direct`
4. `Confirmed Updates — Competitor Relevant`
5. `Competitor Map Snapshot`
6. `Checked Source Log`
7. `Unverified Leads`
8. `Coverage Gaps`
9. `Omission Audit`
10. `검증 메모`

운영 규칙:

- 빈 섹션은 항상 `- 없음`
- 표/JSON/코드블록 금지
- 날짜/시간 형식: `YYYY-MM-DD HH:MM KST`
- `확인 불가`를 `업데이트 없음`으로 바꾸지 않음

## Configuration

### Environment

`server/.env.example`를 기준으로 `.env`를 구성한다.

필수 HanAll 관련 env:

- `GEMINI_API_KEY`
- `SEC_API_KEY`
- `OPENDART_API_KEY`
- `OPENFDA_API_KEY`
- `DATA_GO_KR_API_KEY`
- `NCBI_API_KEY`
- `NCBI_TOOL_NAME`
- `NCBI_EMAIL`

참고:

- `GOOGLE_API_KEY`는 legacy fallback 호환용이다.
- OpenAI 계열 기능도 같이 쓰는 경우 `OPENAI_API_KEY`를 유지한다.

### Source Registry

공식 API collector와 RSS feed는 `server/hanall_sources.yaml`에서 제어한다.

예시:

- 전체 source 비활성화: `collectors.sec_api.enabled: false`
- RSS feed 비활성화: `rss.feeds[*].enabled: false`
- MFDS dataset별 승인 전 유지: `collectors.mfds.enabled: false`, `collectors.mfds.services.*: false`

### MFDS / data.go.kr 운영 주의

MFDS 계열 endpoint는 dataset별 승인 상태가 다를 수 있다.

- collector 전체가 꺼져 있으면 source log / coverage gap에 disabled 상태가 남는다.
- 승인 전에는 `collectors.mfds.enabled: false`를 유지한다.
- 승인 후에도 dataset별로 `services.<dataset>: true`를 개별 활성화한다.
- 403/404/429는 collector가 삼키지 않고 source log / coverage gap에 반영한다.

## Fallback Behavior

- stage1 실패: 공식 API raw findings 기반 structured fallback 생성
- stage2 실패: stage1 JSON + source log + coverage gaps 기반 deterministic text 렌더
- collector 실패: 전체 브리핑은 계속 생성하고, 실패는 source log / coverage gap에 남김

## Delivery Mode

현재 delivery layer는 polling-only 운영을 전제로 한다.

- active transport: `polling_outbox`
- canonical server endpoints: `POST /kakao/polling/pull`, `POST /kakao/polling/ack`
- `/kakao/outbox/*`와 `/kakao/socket/*` 경로는 호환용 alias 또는 deprecated 상태 확인용으로만 남긴다.
- socket push는 활성 delivery 경로가 아니다.

## Running Tests

프로젝트 루트에서 실행:

```bash
/root/kakao_bot/venv/bin/python -m compileall server
/root/kakao_bot/venv/bin/python -m unittest discover -s server/tests -p 'test_*.py'
```

fixture 기반 한올 collector 회귀 테스트만 빠르게 돌릴 때:

```bash
cd /root/kakao_bot
/root/kakao_bot/venv/bin/python -m unittest server.tests.test_hanall_collectors
```

live API smoke test는 기본적으로 skip된다. 운영 검증 시에만 아래처럼 켠다:

```bash
cd /root/kakao_bot
RUN_HANALL_LIVE_SMOKE=1 /root/kakao_bot/venv/bin/python -m unittest server.tests.test_hanall_live_smoke
```

source별 auth/method/query 진단만 빠르게 보고 싶을 때:

```bash
cd /root/kakao_bot
set -a
source /root/kakao_bot/server/.env
set +a
/root/kakao_bot/venv/bin/python -m server.tests.hanall_source_probe
```

MFDS/data.go.kr approval-gated dataset까지 포함할 때:

```bash
cd /root/kakao_bot
RUN_HANALL_LIVE_SMOKE=1 HANALL_LIVE_MFDS_SERVICES=drug_product_permission /root/kakao_bot/venv/bin/python -m unittest server.tests.test_hanall_live_smoke
```

## Operational Notes

### MVP Source Status

- `sec_api.query_api`, `sec_api.form_8k`, `sec_api.insider_trading`: enabled. POST JSON + `Authorization` header probe가 통과했다.
- `sec_api.sec_litigation_releases`: enabled. 현재 probe 기준으로는 `200 no_match`였고, route/method 오류는 아니다.
- `opendart`: enabled. `corpCode.xml` 최소 probe가 통과했고 zip/원시 XML error body 둘 다 파싱한다.
- `cris`: enabled. `serviceKey`는 decode-once 후 `requests params`로 보내야 한다.
- `mfds`: disabled by default. approval-gated 정책을 유지하고, dataset별 승인 전까지 `services.*: false`를 유지한다.
- `openfda`: enabled. 현재 keyed probe는 유효했고, enforcement `404 NOT_FOUND / No matches found!`는 `no_match`로 분류한다.
- `clinicaltrials`: enabled. 기존 `@lastUpdatePostDate desc`는 400이었고, `LastUpdatePostDate:desc`로 교정했다.
- `ncbi`: enabled if no_key_mode succeeds else degraded. 현재 정책은 invalid key가 감지되면 그 run 전체를 `forced_no_key`로 전환한다.
- `europe_pmc`: enabled if corrected runtime endpoint/query succeeds else degraded. docs page URL은 runtime endpoint로 쓰지 않는다.
- `crossref`: enabled.
- `biorxiv`: enabled.

- `rooms.yaml`의 `hanall_news_prompt` key는 외부 호환용 legacy 설정일 수 있다.
- 실제 브리핑 생성 경로는 내부적으로 2단계 파이프라인을 사용한다.
- live API 응답 포맷은 source마다 편차가 있으므로 운영 중 parser 튜닝이 추가로 필요할 수 있다.
- 네트워크 제한 환경에서도 pipeline 자체는 fallback과 source log를 통해 실패 원인을 남기도록 설계되어 있다.
- fixture 갱신 절차와 known fragile fields는 `server/tests/fixtures/hanall/README.md`에 정리했다.
