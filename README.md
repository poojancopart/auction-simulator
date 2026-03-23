# Auction Simulator v8

A web-based live car auction simulator modeled after Copart, with real-time AI auctioneer voice (ElevenLabs TTS), competing auto-bidders, and a fully interactive frontend.

---

## Features

- Live auctioneer voice powered by ElevenLabs text-to-speech
- Two competing auto-bidders (Texas 🤠, Las Vegas 🎰) with randomized timing
- You play as California 🌴 — place bids manually
- Filler chatter, vehicle highlights, and countdown timer between bids
- Configurable vehicles, voices, bid increments, and speech timing
- Session reports saved after each auction
- Hot-reload config — edit text files and click Reload without restarting

---

## Requirements

- Python 3.x
- An [ElevenLabs](https://elevenlabs.io) API key

---

## Local Setup

**1. Install dependencies**
```bash
pip install -r requirements.txt
```

**2. Add your ElevenLabs API key**
```bash
cp tokens.example.txt tokens.txt
# Edit tokens.txt and replace YOUR_ELEVENLABS_API_KEY_HERE with your real key
```

**3. Run the app**
```bash
python app.py
```

Open `http://localhost:5006` in your browser.

---

## Configuration Files

All config is in plain text files. Edit them anytime — click **Reload** in the UI to apply changes without restarting (except `voices.txt` and `tokens.txt` which require a restart).

### `tokens.txt`
Stores your ElevenLabs API key. Never commit this file — it's in `.gitignore`.
```
elevenlabs-key = YOUR_KEY_HERE
```

### `lots.txt`
Defines the vehicles available at auction. Each entry is a blank-line-separated block:
```
Vehicle Name
Declaration line (spoken when declarations are enabled)
Highlight line 1 (spoken mid-auction)
Highlight line 2 (spoken mid-auction)
optional_video.mp4
```
Example:
```
2023 Porsche Macan S
All right folks, here we go — a twenty twenty-three Porsche Macan S...
Twin-turbo V6, Sport Chrono package — zero to sixty in four-point-three seconds.
PASM sport suspension, Bose surround sound, panoramic roof — fully loaded.
```

### `voices.txt`
Maps display names to ElevenLabs voice IDs. The dropdown in the UI is built from this file.
```
Auctioneer Clone: wJOIFG9jHbRE5ieiZG4X
Jimmy Landis: xo57OLuptvtTCxTKn0Uw
```

### `auction_templates.txt`
Auctioneer callout templates organized by section. Available variables:
| Variable | Description |
|---|---|
| `{{vehicle}}` | Vehicle name |
| `{{current_bid}}` | Current bid (dollars) |
| `{{next_bid}}` | Next ask price |
| `{{short_current_bid}}` | Shortened spoken form (e.g. "twenty-five hundred") |
| `{{short_next_bid}}` | Shortened spoken form of next ask |
| `{{state}}` | Leading bidder's location name |
| `{{buyer_name}}` | Leading bidder's full name |

Sections: `OPENING_CALLS_NO_DECLARATION`, `OPENING_CALLS_AFTER_DECLARATION`, `BID_ACCEPTED`, `BID_ACCEPTED_UI`, `FILLERS`, `CLOSING`

### `fillers.txt`
One filler line per line — the auctioneer cycles through these between bids to fill silence.
```
Alright folks.. Who's next?
Let's get it going folks.. Anybody else?
```

### `speech_config.txt`
Controls timing and pacing:
| Setting | Default | Description |
|---|---|---|
| `filler_chain_max` | `4` | Max consecutive filler lines after each bid |
| `filler_delay_ms` | `0` | Pause (ms) before fillers start |
| `highlight_delay_seconds` | `4` | Seconds after bid callout before a highlight fires |
| `min_bids_first` | `3` | Minimum bids before the first highlight |
| `bids_between` | `3` | Bids required between highlights |
| `timer_duration_seconds` | `10` | Countdown bar duration |

---

## Adding Vehicle Images

Place images in `images/` and reference them in the UI setup modal by matching the folder name to the vehicle. The image server route is `/images/<path>`.

Example structure:
```
images/
  New UI/
    BMW X Drive- new/
      87250995_Image_1.jpg
      87250995_Image_2.jpg
```

---

## Deployment (Railway)

This app is configured for [Railway](https://railway.app) deployment.

1. Push to GitHub
2. Connect your GitHub repo in Railway → **New Project → Deploy from GitHub**
3. Add environment variable in Railway → **Variables**:
   ```
   ELEVENLABS_KEY = your_api_key_here
   ```
4. Railway auto-deploys on every push to `main`

The app runs with gunicorn in single-worker mode (`--workers 1 --threads 4`) to preserve in-memory auction state across requests.

---

## How an Auction Works

1. Click **START** → select a vehicle, set start price, reserve price, and bid increment
2. The auctioneer opens with a bid call (with or without a declaration)
3. Texas and Las Vegas auto-bid at random intervals (1.5–8.5s) up to their budgets
4. Use the **bid buttons** to place your bids as California
5. The auction ends when:
   - The countdown timer expires with no new bids (passed)
   - The reserve price is met (sold)
   - You click **Force Close**
6. A session report is saved automatically

---

## Project Structure

```
auction_sim_v8/
├── app.py                  # Flask backend — all auction logic, TTS, auto-bidding
├── templates/
│   └── index.html          # Single-page frontend (vanilla JS + CSS)
├── images/                 # Vehicle images served at /images/<path>
├── session_reports/        # Auto-generated auction logs (gitignored)
├── lots.txt                # Vehicle definitions
├── voices.txt              # ElevenLabs voice mappings
├── auction_templates.txt   # Auctioneer callout templates
├── fillers.txt             # Filler lines between bids
├── speech_config.txt       # Timing and pacing settings
├── tokens.txt              # API key (gitignored — copy from tokens.example.txt)
├── tokens.example.txt      # Template for tokens.txt
├── requirements.txt        # Python dependencies
└── railway.toml            # Railway deployment config
```
