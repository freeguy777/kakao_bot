# Kakao Delivery Incident & Maintenance Log

Last updated: 2026-03-28

## Purpose

이 문서는 KakaoTalk 전송 경로에서 발생한 문제, 재현 결과, 우회 시도, 원복 이력, 현재 운영 판단을 누적 기록하는 상시 유지보수 문서다.

이 문서를 보는 사람은 아래를 빠르게 파악할 수 있어야 한다.

- 현재 운영 가능한 안전한 전송 방식
- 최근에 확인된 장애 또는 비정상 동작
- 이미 시도했지만 실패했거나 원복한 접근
- 서버 로그상 성공과 실제 KakaoTalk 수신 성공의 차이
- 다음 유지보수 시 어디부터 다시 보면 되는지

## Update Rules

새 문제가 생기거나 재검증을 할 때는 아래 순서로 갱신한다.

1. `Current Status`를 먼저 최신 판단으로 수정한다.
2. `Known Constraints`에 새 제약이 확인되면 추가한다.
3. 새 장애나 테스트는 `Incident History`에 날짜순으로 추가한다.
4. 이미 시도했다가 실패한 방법은 지우지 말고 `Tried and Reverted` 또는 `Rejected Approaches`에 남긴다.
5. 서버 기준 성공과 실제 KakaoTalk 수신 성공은 반드시 분리해서 적는다.
6. 날짜와 시간은 가능하면 절대값으로 적는다. 예: `2026-03-26 11:02 KST`
7. trace id, 길이, 청크 수, 실제 수신 여부는 생략하지 않는다.

## Record Template

새 항목 추가 시 아래 형식을 복사해서 `Incident History`에 누적한다.

```md
### YYYY-MM-DD HH:MM KST | short title

- Symptom: 실제로 어떤 문제가 보였는지
- Scope: 어느 방/어느 경로/어느 기능에서 발생했는지
- Trigger: 무엇을 보냈고 어떤 조건이었는지
- Server Result: 서버 로그상 결과
- Client Result: 실제 KakaoTalk 수신 결과
- Trace IDs: 관련 trace id
- Hypothesis: 현재 원인 추정
- Action Taken: 시도한 조치
- Decision: 운영상 어떻게 처리하기로 했는지
```

## Delivery Path

현재 서버 기준 전송 경로:

1. `server/application/delivery.py`의 `deliver_room_messages(...)`
2. `message_length_limit` 기준 분할
3. outbox 적재
4. `bot.txt` polling loop의 `/polling/pull`
5. MessengerBot `bot.send(...)`
6. `/polling/ack`
7. KakaoTalk 표시

관련 파일:

- `server/application/delivery.py`
- `server/infra/sqlite_store.py`
- `server/main.py`
- `server/config.py`
- `server/rooms.yaml`
- `server/application/youtube.py`
- `bot.txt`

## Current Status

현재 기준 판단:

- 서버의 `ok=true`는 실제 KakaoTalk 수신 성공을 의미하지 않는다.
- socket push는 기본 운영 경로에서 제외되었고, 현재 기본 전달 방식은 polling/outbox다.
- polling/outbox 경로에서는 관리자방 기준 `3000`자 단일 메시지 실제 수신이 확인됐다.
- 기본 `message_length_limit`은 현재 `3000`이다.
- 긴 메시지 분할은 문단 경계를 유지하되, 현재 청크를 최대한 채우는 방식으로 보정됐다.
- phone-side 일회성 비동기 작업은 raw `startThread()` 남발 대신 bounded async worker queue로 교체됐다.
- `@폴링상태`는 polling loop 상태 외에 async worker/queue 진단값도 함께 보여준다.
- `bot.send(...)`가 실패로 판정돼도 실제 KakaoTalk 방에는 메시지가 도착하는 false negative가 관측됐다.

현재 열려 있는 유지보수 이슈:

- polling thread가 메신저봇R 재시작/스크립트 재로딩 후 안정적으로 재기동되는지 실운영 검증 필요
- outbox ack 실패 시 inflight 재처리와 중복 전송 가능성에 대한 운영 관찰 필요
- 길이 제한보다 polling 주기, ack 안정성, room 식별 정보 품질이 더 중요한 운영 포인트가 됨
- Android 또는 메신저봇 앱 프로세스가 일정 시간 뒤 죽었다가 다시 살아나는지 별도 관찰 필요

현재 운영 권장:

- 서버 주도 메시지는 `deliver_room_messages(...)`로 직접 전송하지 말고 outbox 적재 흐름을 기준으로 본다.
- 메신저봇R에서는 polling loop가 살아 있는지 `@폴링상태`로 먼저 확인한다.
- 긴 메시지는 우선 `3000`자까지 단일 메시지로 유지하고, 초과 시에만 분할한다.
- `@폴링상태` 확인 시 `lastStartedAt`, `lastPolledAt`, `asyncQueueSize`, `asyncDroppedCount`, `asyncLastError`를 같이 본다.

## Known Constraints

현재까지 확인된 제약:

- socket-level success와 KakaoTalk UI 수신 성공은 다르다.
- debugRoom/control payload 자체의 크기 또는 처리 방식이 추가 제약일 수 있다.
- phone-side bridge에서 `ack`가 오지 않거나 `null`이어도 서버는 실패로 취급하지 않는다.
- 청크별 길이가 안전 범위 안이어도, 전체 control payload 경로에서 실패할 수 있다.
- `bot.send(...)`가 false를 반환해도 실제 KakaoTalk 수신이 성공할 수 있다.
- phone-side에서 작업마다 raw thread를 만드는 구조는 장시간 실행 시 메신저봇 안정성을 해칠 수 있다.

## Confirmed Findings

### 1. Server success is weaker than end-to-end delivery success

확인 내용:

- `server/infra/socket_push.py`는 socket write가 되면 `ok=true`를 반환한다.
- 이 값만으로는 phone-side bot parse 성공이나 KakaoTalk 표시 성공을 보장할 수 없다.

유지보수 시 주의:

- 장애 확인 시 서버 응답만 보고 "전송 성공"으로 판단하면 안 된다.

### 2. Practical safe single-message limit is about 1800 characters

확인 내용:

- 초기 테스트에서 `1800`자 단일 메시지는 실제 수신됨.
- 이후 `1900`, `2000`, `2400`, `3000` 단일 메시지는 실제 수신 실패.
- 이후 재검증에서는 `1800`자 단일 메시지도 실제 수신 실패가 관측됨.

유지보수 시 주의:

- 현재는 `1800`을 안정 상한으로 확정하면 안 된다.
- 길이 상한을 논하기 전에 phone-side bridge 안정성부터 다시 확인해야 한다.

### 5. Polling/outbox path supports longer single-message delivery than legacy socket tests

확인 내용:

- polling/outbox 전환 후 관리자방 기준 `2000`자 분할 전송과 `3000`자 단일 메시지 전송이 실제 처리됐다.
- `3000`자 단일 메시지는 outbox `sent` 상태와 실제 관리자방 수신으로 확인했다.

유지보수 시 주의:

- 이 결과는 현재 polling/outbox 경로 기준이다.
- 예전 socket/debugRoom 길이 테스트 결과와 직접 섞어서 해석하면 안 된다.

### 3. Chunking alone did not solve long-message delivery

확인 내용:

- `2100`, `2400`, `3000` 메시지를 `1800` 이하 청크로 분할해도 실제 수신 실패가 발생했다.
- 예시 청크:
  - `119 / 1800 / 180`
  - `119 / 1800 / 480`
  - `119 / 1800 / 1080`

원인 추정:

- 최종 Kakao 메시지 크기뿐 아니라, debugRoom으로 보내는 control payload 크기와 parse 단계도 제약이다.

### 4. `ack` is not a reliable delivery proof

확인 내용:

- 일부 테스트에서는 `ack` 문자열이 있었고, 일부는 `null`이었다.
- `ack`가 있어도 실제 수신을 보장하지 않았고, `ack=null`도 자주 관측됐다.

유지보수 시 주의:

- `ack`는 보조 신호로만 보고, 실제 수신 여부를 따로 확인해야 한다.

## Active Configuration Notes

현재 유지 중인 설정 관련 판단:

- `message_length_limit` 기본값은 `3000`
- 이 값은 현재 polling/outbox 경로 기준 운영 기본값이다.
- `hanall_news_prompt`의 `temperature` 설정은 제거됨
- `hanall_news_brief`는 프롬프트 강제만이 아니라 OpenAI `json_schema` Structured Outputs로 응답 형식을 강제한다.
- `HANALL_NEWS_MAX_OUTPUT_TOKENS` 기본값은 현재 `3200`이다.

LLM 관련 메모:

- `gpt-5-mini` + tools 경로에서는 `temperature`가 실효 설정이 아니었다.
- 현재는 프롬프트 제약과 JSON 검증으로 일관성을 유지한다.

## Tried and Reverted

### Socket-first delivery path

시도 내용:

- FastAPI가 phone-side socket/debugRoom 브리지로 직접 push 전송

결과:

- 소켓 레벨 성공과 실제 KakaoTalk 수신이 일치하지 않았음
- 길이와 무관하게 bridge 응답이 흔들렸고 재현성이 낮았음
- 운영 경로로 유지하기 어렵다고 판단

현재 상태:

- 기본 운영 경로에서 제외
- 관련 코드는 당분간 참고용/호환성용으로만 잔존

### Sequential per-chunk socket requests

시도 내용:

- 여러 청크를 한 번의 control payload로 묶지 않고, 각 청크를 독립 요청으로 보내는 방식 시도

왜 시도했는가:

- 큰 wrapped control payload를 피하기 위해

결과:

- `3000`자 전달 문제를 해결하지 못함
- phone-side bridge와의 호환성 리스크가 확인됨
- `1800` 재검증까지 흔들려서 최종 원복

현재 상태:

- 이 실험은 폐기
- 서버 코드는 원래 bundled `send_messages` 방식으로 복귀

## Rejected Approaches

현재 시점에서 운영 기본안으로 채택하지 않는 접근:

- `1800` 초과 메시지를 단순 분할만 해서 보내는 방식
- 서버 `ok=true`만으로 성공 판단하는 방식
- phone-side end-to-end 확인 없이 상한을 올리는 방식

## Incident History

### 2026-03-28 23:29 KST | Phone-side thread accumulation suspected, async worker queue introduced

- Symptom: 관리자방 가족 브리핑이 오랫동안 `pending`으로 남았고, 폰 쪽 메신저봇이 죽었다가 다시 살아난 것처럼 보였다.
- Scope: polling/outbox 기반 관리자방 발송 경로와 phone-side `bot.txt`
- Trigger: `2026-03-28` 저녁에 관리자방 가족 브리핑을 수동 예약 적재했지만 운영 DB에서 `last_attempt_at=None` 상태가 길게 유지됐다.
- Server Result: 서버 스케줄러의 `rooms_config_watch`는 정상 heartbeat를 유지했고, 가족 브리핑 outbox `58`, `59`가 적재됐다.
- Client Result: 이후 polling이 다시 살아난 뒤 관리자방에 대기 중이던 가족 브리핑 2건이 실제 도착했다. 추가로 오래된 잘못된 outbox `id=29`가 계속 pull돼 ack fail 카운트를 더럽히고 있었다.
- Trace IDs: `f3cd3ead488b`, `31e91ea7e727`
- Hypothesis: phone-side에서 관리자 알림, 포워딩, 수동 동기화 등 일회성 작업마다 raw thread를 만드는 구조가 누적돼 장시간 실행 안정성을 해쳤을 가능성이 높다.
- Action Taken: `bot.txt`의 one-shot async 경로를 `2`개 worker와 `200`개 용량의 bounded queue로 교체하고, `@폴링상태`에 `asyncWorkersStarted`, `asyncQueueSize`, `asyncEnqueuedCount`, `asyncExecutedCount`, `asyncDroppedCount`, `asyncLastError`를 추가했다. 오래된 잘못된 outbox `id=29`는 `cancelled`로 정리했다.
- Decision: polling loop는 전용 장수 스레드로 유지하고, 일회성 비동기 작업은 raw thread-per-task를 사용하지 않는다. `bot.send(...)` 실패 로그는 실제 KakaoTalk 수신 여부와 분리해서 해석한다.

### 2026-03-26 | Initial investigation and baseline documentation

작업:

- `prompts.yaml` 들여쓰기 오류 수정
- `hanall_news_prompt` 응답을 JSON 검증 후 KakaoTalk용 텍스트로 렌더링하도록 변경
- `message_length_limit` 기본값을 `900 -> 1800`으로 상향
- 1800/1900/2000/2100/2400/3000 길이 테스트 수행
- 청크 순차 개별 전송 방식 실험 후 원복

테스트 결과 요약:

- `1800` 단일 메시지: 초기 테스트에서 실제 수신 성공
- `1900` 단일 메시지: 실제 수신 실패
- `2000` 단일 메시지: 실제 수신 실패
- `2400` 단일 메시지: 실제 수신 실패
- `3000` 단일 메시지: 실제 수신 실패
- `2100+` 분할 메시지: 실제 수신 실패

대표 trace id:

- `fa5f17a21178`: 1800자 테스트
- `02819deb78f8`: 3000자 분할 테스트
- `f90a11af47ff`: 2400자 분할 테스트
- `53ac13dba920`: 2100자 분할 테스트
- `ee0fd7a23b7f`: 3000자 단일 메시지 테스트
- `9c1354c6f964`: 2400자 단일 메시지 테스트
- `cf94f4ec0518`: 2000자 단일 메시지 테스트
- `54144f58b202`: 1900자 단일 메시지 테스트

최종 판단:

- 현재 전송 경로의 실전 안전 상한은 `1800`
- 그 이상은 추가 조사 전까지 운영에 사용하지 않음

### 2026-03-26 | 1800-character retest after app restart failed

작업:

- 앱 재시작 후 `1800`자 단일 메시지 재검증 수행
- 동일한 `deliver_room_messages(...) -> send_messages` 경로로 전송
- 관리자 방 실제 수신 여부 재확인

테스트 결과 요약:

- `1800` 단일 메시지 재검증 1차: 서버 전송 성공, `ack`에 debugRoom payload 유사 응답 수신, 실제 KakaoTalk 수신 실패
- `1800` 단일 메시지 재검증 2차: 서버 전송 성공, `ack=null`, 실제 KakaoTalk 수신 실패
- 즉, 동일 길이와 동일 경로에서도 bridge 응답 상태가 일관되지 않았고 실제 수신도 재현되지 않음

대표 trace id:

- `f1fb1e6a87ab`: 1800자 단일 메시지 재검증, `ack=null`, 실제 수신 실패
- `ca9024a9b28a`: 앱 재시작 후 1800자 단일 메시지 재검증, `ack` 수신, 실제 수신 실패
- `3032322c376b`: 1800자 단일 메시지 재재검증, `ack=null`, 실제 수신 실패

최종 판단:

- 현재는 `1800`도 신뢰 가능한 운영 상한으로 볼 수 없음
- 서버 응답과 socket `ack`만으로는 실제 KakaoTalk 수신 여부를 판단할 수 없음
- 문제의 중심은 길이 제한 단독 이슈보다 phone-side MessengerBot/debugRoom 브리지 안정성일 가능성이 큼

### 2026-03-26 | Added phone-side diagnostic logging

작업:

- `bot.txt`에 socket control parse 실패 원인 세분화 추가
- `bot.canReply(...)` 결과와 예외를 trace id 기준으로 기록하도록 추가
- `bot.send(...)` 실패 시 몇 번째 메시지와 길이에서 실패했는지 기록하도록 추가
- 관리자 디버그 알림에 message count, 각 메시지 길이, total chars, preview를 포함하도록 추가

검증:

- `bot.txt`를 임시 `.js` 파일로 복사한 뒤 `node --check`로 문법 확인 완료

기대 효과:

- 다음 장애 발생 시 `parse failed`, `canReply returned false`, `bot.send returned false`, 예외 발생 지점을 더 직접 식별 가능
- 서버 성공과 phone-side 실패 사이의 공백을 로그로 좁힐 수 있음

### 2026-03-26 | Switched default delivery mode to polling/outbox

작업:

- `AGENT.md`를 polling/outbox 기준 문서로 전면 수정
- `server/application/delivery.py`를 socket-first가 아니라 queue-first 동작으로 변경
- FastAPI에 `polling/pull`, `polling/ack` endpoint 추가
- `bot.txt`에 polling loop, pull/ack 처리, `@폴링상태/@폴링시작/@폴링중지/@폴링재시작/@동기화` 관리자 명령 추가
- scheduler의 socket flush job 제거

검증:

- `./venv/bin/python -m py_compile ...` 통과
- `./venv/bin/python -m unittest server.tests.test_services` 통과
- `bot.txt`를 임시 `.js`로 복사해 `node --check` 통과

운영 판단:

- 앞으로 서버 주도 메시지 전달의 기준 경로는 polling/outbox
- socket push는 기본 운영안으로 보지 않음

### 2026-03-26 | Raised default chunk limit to 3000 and improved paragraph packing

작업:

- `message_length_limit` 기본값을 `1800 -> 3000`으로 상향
- delivery 분할 로직을 수정해 앞 문단 뒤에 긴 문단이 이어질 때 현재 청크를 최대한 채우도록 변경
- 관리자방에서 `2000`자 분할 전송과 `3000`자 단일 메시지 전송 재검증

테스트 결과 요약:

- `2000`자 메시지: `21 / 1800 / 178`로 분할되어 polling `sent`
- `3000`자 단일 메시지: outbox 1건으로 `sent`, 실제 관리자방 수신 확인
- 분할 알고리즘 보정 후에는 `1800 / 200`처럼 더 자연스럽게 채울 수 있도록 테스트 추가

대표 trace id:

- `20e0647cf398`: 2000자 polling 분할 테스트
- `b3d2d8ae9685`: 3000자 polling 단일 메시지 테스트

최종 판단:

- polling/outbox 기준 기본 운영 한도는 `3000`으로 올려도 된다.
- 초과 메시지는 계속 분할하되, 청크 효율이 떨어지는 기존 문단 분리 문제는 보정됐다.

### 2026-03-26 | Switched Hanall news prompt to Structured Outputs

작업:

- `hanall_news_brief` OpenAI 호출에 `text.format = json_schema`와 `strict: true` 적용
- 스키마는 서버의 `HanallNewsReport` Pydantic 모델에서 생성하도록 변경
- structured output 응답이 `incomplete`이면 partial text를 바로 쓰지 않고 더 큰 token budget으로 재시도하도록 변경
- `HANALL_NEWS_MAX_OUTPUT_TOKENS` 기본값을 `3200`으로 상향

검증:

- 단위 테스트 19개 통과
- 실제 OpenAI 호출에서 `3200`, `4800` budget은 `incomplete`였지만 재시도 후 완료 응답 수신
- 실제 렌더링된 한올 뉴스 브리프 길이: `719`자

운영 판단:

- 앞으로는 프롬프트 문구만으로 JSON을 강제하지 않고, API-level structured output을 기본으로 사용한다.
- `incomplete`가 발생해도 partial JSON을 내려보내지 않고 재시도하므로 응답 깨짐 위험이 줄어든다.

### 2026-03-26 | Reduced Hanall news output schema to compact verification summary

작업:

- `checked_source_log`, `coverage_gaps`, `omission_audit`, `competitor_map_snapshot`를 최종 출력 스키마에서 제거
- 대신 `verification` 객체에 `checked_source_count`, `checked_source_examples`, `access_limitations`, `coverage_gap_summary`, `universe_notes`로 압축
- 핵심 이벤트 배열(`confirmed_company`, `confirmed_competitor`, `unverified_leads`)은 유지

검증:

- 실제 OpenAI 호출 성공
- 최종 렌더링 길이: `1012`자
- raw compact JSON은 관리자방 사전 전송 대상

운영 판단:

- 조사 범위는 유지하되, 최종 structured output은 핵심 이벤트 + 압축 검증 요약으로 제한하는 편이 안정적이다.

## Next Investigation Checklist

다음에 이 영역을 다시 볼 때 우선순위:

1. 메신저봇R 재시작 후 polling loop가 자동으로 다시 살아나는지 `@폴링상태`로 확인한다.
2. `pending/inflight/sent` outbox 카운트와 실제 카카오 수신이 일치하는지 운영 중 관찰한다.
3. ack 실패 시 inflight 재처리와 중복 전송 여부를 실제 운영 로그로 확인한다.
4. room snapshot이 없는 상태에서도 scheduled room 전송이 안정적인지 검증한다.
5. `@폴링상태`의 `asyncQueueSize`, `asyncDroppedCount`, `asyncLastError`가 누적되거나 비정상적으로 증가하는지 본다.
6. `lastStartedAt`이 자주 바뀌는지 보고 Android 백그라운드 프로세스 종료 여부를 추적한다.
