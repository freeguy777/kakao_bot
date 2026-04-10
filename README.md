# Kakao Bot MVP

Single-app Kakao bot system built with MessengerBotR API2, FastAPI, SQLite, and APScheduler.

## Components
- `bot_gateway.txt`
  - MessengerBotR API2 gateway script running on the smartphone
  - Forwards normal Kakao inbound messages to `POST /kakao/webhook`
  - Intercepts built-in socket `debugRoom` control messages and executes `bot.send(...)`
  - Does not open a custom socket server
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
4. In MessengerBot app settings, enable the built-in socket server and set the port to match `SMARTPHONE_SOCKET_PORT`.
5. Before rollout, manually verify that a documented `debugRoom` envelope reaches API2 `Event.MESSAGE` with the expected `isDebugRoom`, `room`, `author.name`, `content`, and `packageName` fields on the installed app version.

## Required Environment Variables
- `INBOUND_BOT_SECRET`
- `SOCKET_SHARED_TOKEN`
- `MESSENGERBOT_BOT_NAME`
- `MB_SOCKET_CONTROL_AUTHOR_NAME`
- `MB_SOCKET_CONTROL_ROOM_NAME`
- `SMARTPHONE_HOST`
- `SMARTPHONE_SOCKET_PORT`
- `GEMINI_API_KEY`
- `KIMI_API_KEY`
- `WEATHER_API_KEY`

## Admin Commands
- `@상태`
- `@방목록`
- `@전송큐`
- `@소켓상태`
- `@진단`
- `@재전송 <message_id>`
- `@기능조회 <room>`
- `@기능설정 <room> <feature> on`
- `@기능설정 <room> <feature> off`

## Notes
- Normal feature output stays in user rooms. Operational and error output stays in the admin room only.
- Inbound dedupe uses `logId`, outbound dedupe uses `message_id`, scheduler dedupe uses `job_key`.
- Outbound socket success means only that FastAPI wrote the control envelope to MessengerBot's built-in socket transport.
- Device-side `bot.send()` failures are reported locally by `bot_gateway.txt` to the admin room instead of returning a socket ACK to FastAPI.
- HanAll collect runs once at 07:42 KST and room publish jobs reuse the stored artifact.

## MessengerBotR Docs
- [Event](https://violetxf.gitbook.io/messengerbot/api2/event)
- [Database](https://violetxf.gitbook.io/messengerbot/api2/database)
- [Socket Communication](https://violetxf.gitbook.io/messengerbot/tips/socket-communication)
