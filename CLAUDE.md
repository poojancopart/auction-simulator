# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the App

```bash
python app.py
```

Starts the Flask server at `http://localhost:5006`. No build step needed. Dependencies: `flask`, `elevenlabs`.

## Architecture Overview

**Single-file Flask backend** (`app.py`, ~1136 lines) + **Single-page HTML frontend** (`templates/index.html`).

### Backend (`app.py`)

All auction state lives in a single global `auction` dict. The main components:

- **Auto-bidding engine**: Background threads (`threading.Timer`) schedule bids from Texas and Las Vegas auto-bidders at random 1.5–8.5s delays. Entry point: `_schedule_auto_bid()` → `_do_auto_bid()`.
- **Event queue**: `_add_event()` pushes state snapshots + base64 audio into `auction["events"]`. Frontend polls `/auction/events` every 400ms to drain this queue.
- **TTS integration**: `tts(text, speed)` calls ElevenLabs API and returns base64 MP3. All auctioneer speech flows through here.
- **Template system**: `_render_template()` picks from `auction_templates.txt` sections and substitutes variables (bid amounts in spoken English via `_int_to_words()`, bidder names, etc.).
- **Session logging**: `_log()` → `_generate_report()` → `_save_report()` writes `.txt` reports to `session_reports/`.

### Frontend (`templates/index.html`)

Vanilla JS with no framework. Key pieces:

- `pollEvents()` — runs every 400ms, calls `handleAutoEvent()` for each queued auto-bid event.
- `render()` — redraws the entire UI from current state (3-column layout: media | details | upcoming lots).
- Audio pipeline: `playAudio()` → `enqueueAudio()` → `runFillerChain()` — all audio is base64 MP3 from backend.
- `startTimer()` / `stopTimer()` — countdown circle (default 10s); auto-closes auction on expiry.

### Auction Lifecycle

1. `POST /auction/start` — initializes state, generates opening call audio, sets auto-bidder budgets (95–98% of reserve).
2. `POST /auction/ready` — called after opening audio finishes; schedules first auto-bid.
3. Bidding loop: auto-bidders fire via timers; California (player) bids via `POST /auction/bid`.
4. Auction closes via timer expiry, manual `POST /auction/close`, or reserve not met.
5. Session report saved to `session_reports/`.

### Three Bidders

| ID | Emoji | Type |
|---|---|---|
| `texas` | 🤠 | Auto (background timer) |
| `vegas` | 🎰 | Auto (background timer) |
| `california` | 🌴 | Human player |

## Configuration Files (no code changes needed)

All text files, hot-reloadable via `POST /reload-config`:

- `tokens.txt` — ElevenLabs API key
- `voices.txt` — TTS voice ID mappings
- `lots.txt` — Vehicle definitions (declarations, highlights, image/video paths)
- `auction_templates.txt` — Auctioneer callout templates by section (`OPENING_CALLS_*`, `BID_ACCEPTED`, `CLOSING`, etc.)
- `fillers.txt` — Ad-lib filler lines
- `speech_config.txt` — Timing parameters (auto-bid delay range, filler chain length, highlight intervals, timer duration)

## Key API Routes

| Method | Route | Purpose |
|---|---|---|
| `POST` | `/auction/start` | Initialize auction |
| `POST` | `/auction/ready` | Start auto-bidding after opening audio |
| `POST` | `/auction/bid` | California manual bid |
| `GET` | `/auction/events` | Poll auto-bid events |
| `POST` | `/auction/close` | Force hammer down |
| `POST` | `/auction/reset` | Return to idle |
| `POST` | `/auction/filler` | Request filler line |
| `POST` | `/auction/highlight` | Request vehicle highlight |
| `POST` | `/reload-config` | Hot-reload all config files |
| `GET` | `/lots` | List vehicles |
| `GET` | `/voices` | List TTS voices |
