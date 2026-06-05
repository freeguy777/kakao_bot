# Kakao Bot MVP

Single-app Kakao bot system built with MessengerBotR API2, FastAPI, SQLite, and APScheduler.

## Components
- `bot_gateway.txt`
  - MessengerBotR API2 gateway script running on the smartphone
  - Forwards normal Kakao inbound messages to `POST /kakao/webhook`
  - Polls `POST /kakao/polling/pull`, executes `bot.send(...)`, then ACKs `POST /kakao/polling/ack`
  - Built-in socket `debugRoom` control handling is disabled by default
- `app/`
  - FastAPI application
  - Room exact-match routing
  - Chat, YouTube summary, HanAll briefing, weather, child-age briefing, admin commands
- `config/`
  - Room config, prompt config, personas

## Run
1. Copy `.env.example` to `.env` and fill in real values.
2. Start FastAPI:
   - `uvicorn app.main:app --host 0.0.0.0 --port 8000`
3. Load `bot_gateway.txt` into MessengerBotR.
4. Confirm `bot_gateway.txt` has the correct `webhookUrl`, `pollingPullUrl`, `pollingAckUrl`, and `botSecret`.
5. The MessengerBotR built-in socket/debugRoom server does not need to be enabled for the default outbound path.

## Required Environment Variables
- `INBOUND_BOT_SECRET`
- `GEMINI_API_KEY`
- `KIMI_API_KEY`
- `WEATHER_API_KEY`

Legacy socket variables such as `SOCKET_SHARED_TOKEN`, `MESSENGERBOT_BOT_NAME`, `SMARTPHONE_HOST`, and `SMARTPHONE_SOCKET_PORT` are still accepted for compatibility, but startup no longer requires them for polling outbox delivery.

## Delivery Flow
See [docs/outbox_delivery.md](docs/outbox_delivery.md) for the detailed polling outbox contract.

- Inbound stays unchanged: Kakao message -> `bot_gateway.txt` -> `POST /kakao/webhook`.
- Outbound is now polling outbox: server schedule/command -> `outbound_messages` row with `pending` status -> `bot_gateway.txt` pulls -> `bot.send()` -> polling ACK.
- `DeliveryService.send_text()` splits long text into chunks, inserts one `pending` row per chunk, and returns `ok` once the DB enqueue succeeds.
- The server no longer treats immediate Kakao send success as part of `send_text()`; real device delivery is reflected later by `/kakao/polling/ack`.
- `retry_message()` moves an existing message back to `pending`, so the next polling pull can deliver it.
- Failed ACKs and expired rows are not automatically requeued; use explicit admin retry only when a message should still be sent.

## Polling API
- `POST /kakao/polling/pull`
  - Requires `X-Bot-Secret`.
  - Request: `{"limit": 5}`.
  - Returns oldest `pending` messages up to `limit`, and marks returned rows as `inflight`.
  - Response items include `message_id`, `target_room`, `package_name`, `text`, `chunk_index`, `total_chunks`, and `created_at`.
- `POST /kakao/polling/ack`
  - Requires `X-Bot-Secret`.
  - Request: `{"message_id": "...", "success": true, "error_code": null, "error_message": null}`.
  - `success=true` marks the row `sent`.
  - `success=false` records the error, increments attempt tracking, and marks the row `failed` without automatic requeue.
- Fresh `inflight` rows older than five minutes are eligible for the next pull.
- `pending` or stale `inflight` rows older than ten minutes expire as `failed` with `outbound_expired` and are not returned by pull.

## Gateway Runtime
- `bot_gateway.txt` starts a polling worker by default with `pollingIntervalMs=2000` and `pollingBatchLimit=5`.
- For each pulled item, the gateway checks `bot.canReply(...)`, calls `bot.send(...)`, then ACKs success or failure.
- `enableSocketControl` defaults to `false`; debugRoom control messages are ignored unless this is explicitly re-enabled.
- `gateway_diag.json` stores only the current diagnostic snapshot and a bounded recent event list.
- `@게이트웨이진단` and `@gateway_diag` report polling state including `polling_running`, `last_pull_at`, `last_ack_at`, last error, and server-reported pending/inflight counts.

## Admin Commands
- `@상태`
- `@방목록`
- `@전송큐`
- `@소켓상태` returns that socket delivery is disabled and polling outbox is active.
- `@진단`
- `@재전송 <message_id>`
- `@기능조회 <room>`
- `@기능설정 <room> <feature> on`
- `@기능설정 <room> <feature> off`

## Notes
- Normal feature output stays in user rooms. Operational and error output stays in the admin room only.
- Inbound dedupe uses `logId`, outbound dedupe uses `message_id`, scheduler dedupe uses `job_key`.
- FastAPI outbound success means the message was queued in `outbound_messages`; actual device send results arrive later through polling ACKs.
- `outbound_messages.status` uses `pending`, `inflight`, `sent`, and `failed`; runtime schema adds `inflight_at` and `attempt_count` when needed.
- Stale `inflight` outbound rows become eligible for polling again after five minutes without an ACK.
- `pending` or stale `inflight` rows older than ten minutes are expired as `failed` so delayed room recognition cannot flush old messages later.
- HanAll collect runs once at 07:42 KST and room publish jobs reuse the stored artifact.

## MessengerBotR Docs
- [Event](https://violetxf.gitbook.io/messengerbot/api2/event)
- [Database](https://violetxf.gitbook.io/messengerbot/api2/database)
