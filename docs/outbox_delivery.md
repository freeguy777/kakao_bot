# Polling Outbox Delivery

This document describes the default outbound delivery path.

## Summary

- Inbound remains unchanged: Kakao event -> `bot_gateway.txt` -> `POST /kakao/webhook`.
- Outbound uses polling outbox: FastAPI schedules or commands enqueue rows in `outbound_messages`; `bot_gateway.txt` polls, sends through `bot.send()`, then ACKs.
- FastAPI delivery success means DB enqueue success. Device send success or failure is recorded later through polling ACK.
- The MessengerBotR built-in socket/debugRoom path is retained only as legacy code and is disabled by default in `bot_gateway.txt`.

## Server Behavior

`DeliveryService.send_text()`:

- Splits text using the configured chunk limit.
- Inserts one `outbound_messages` row per chunk.
- Creates rows with `status='pending'`.
- Returns `DeliveryResult(status='ok')` after enqueue succeeds.
- Does not call `SocketClient.send_message()` in the default path.

`DeliveryService.retry_message(message_id)`:

- Finds the existing outbound row.
- Clears active delivery state and sets `status='pending'`.
- Returns `ok` so the next polling pull can retry delivery.

`outbound_messages` state:

- `pending`: eligible for polling.
- `inflight`: returned by pull and waiting for ACK.
- `sent`: ACKed as success by the phone.
- `failed`: terminal failure state for failed ACKs, expired rows, and legacy/manual failures. Only explicit admin retry moves it back to `pending`.

Runtime SQLite schema adds these columns when missing:

- `inflight_at`: timestamp when a row was handed to the polling gateway.
- `attempt_count`: count of polling ACK attempts recorded for the row.

## Polling API

All polling endpoints require `X-Bot-Secret` equal to `INBOUND_BOT_SECRET`.

### `POST /kakao/polling/pull`

Request:

```json
{"limit": 5}
```

Behavior:

- Selects oldest `pending` rows up to `limit`.
- Also selects `inflight` rows with no ACK for at least five minutes.
- Does not return rows older than the outbound expiry window.
- Marks returned rows `inflight` and sets `inflight_at` in the same repository operation.

Response item fields:

```json
{
  "message_id": "...",
  "target_room": "...",
  "package_name": "com.kakao.talk",
  "text": "...",
  "chunk_index": 1,
  "total_chunks": 1,
  "created_at": "..."
}
```

### `POST /kakao/polling/ack`

Request:

```json
{
  "message_id": "...",
  "success": true,
  "error_code": null,
  "error_message": null
}
```

Behavior:

- `success=true`: marks the row `sent`, clears last error fields, and records a successful attempt.
- `success=false`: records `error_code` and `error_message`, increments attempt tracking, clears `inflight_at`, and marks the row `failed`.
- Unknown `message_id` returns `404`.
- Missing or invalid `X-Bot-Secret` returns `401`.

Expiry and retry policy:

- `pending` rows older than ten minutes are marked `failed` with `error_code='outbound_expired'` before each pull.
- `inflight` rows older than ten minutes are also expired once they are stale enough to be eligible for reclaim.
- Expired rows are not returned by pull.
- Failed ACKs are not automatically requeued. This prevents old messages from being delivered in bulk after a room becomes replyable again.
- Use explicit admin retry only for messages that should still be sent.

## Gateway Behavior

`bot_gateway.txt` defaults:

- `pollingIntervalMs: 2000`
- `pollingBatchLimit: 5`
- `enableSocketControl: false`

For each pulled message:

1. Validate `message_id`, `target_room`, and `text`.
2. If `message_id` was already processed locally, ACK success.
3. Check `bot.canReply(target_room, package_name)`.
4. Call `bot.send(target_room, text, package_name)`.
5. ACK success or failure to `/kakao/polling/ack`.

The gateway stores diagnostics in `gateway_diag.json` as a bounded snapshot, not an unbounded log.

Admin gateway commands:

- `@게이트웨이진단`
- `@gateway_diag`

The diagnostic report includes polling running state, last pull/ACK timestamps, last error, and server-reported pending/inflight counts.

## Operational Notes

- Do not enable MessengerBotR built-in socket server for the default delivery path.
- Keep public room messages and admin operational messages separate.
- Keep exact room-name matching.
- Outbound idempotency is still based on `message_id`; inbound idempotency remains based on `logId`.
- HanAll and weather messages for multiple rooms enqueue as separate rows and are delivered by polling in DB creation order.
- Failed ACKs are terminal by default. Use explicit admin retry only when a message should be sent again.
