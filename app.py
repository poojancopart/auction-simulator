import os
import base64
import random
import re
import datetime
import threading

from flask import Flask, render_template, request, jsonify, send_from_directory
from elevenlabs.client import ElevenLabs
from elevenlabs.types import VoiceSettings

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Currency → spoken words
# ---------------------------------------------------------------------------

_ONES = [
    '', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine',
    'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen',
    'seventeen', 'eighteen', 'nineteen',
]
_TENS = ['', '', 'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety']


def _under_1000(n: int) -> str:
    if n == 0:
        return ''
    if n < 20:
        return _ONES[n]
    if n < 100:
        rest = _ONES[n % 10]
        return _TENS[n // 10] + (' ' + rest if rest else '')
    rest = _under_1000(n % 100)
    return _ONES[n // 100] + ' hundred' + (' ' + rest if rest else '')


def _int_to_words(n: int) -> str:
    if n == 0:
        return 'zero'
    if n < 1_000:
        return _under_1000(n)
    if n < 1_000_000:
        hi = _under_1000(n // 1_000)
        lo = _under_1000(n % 1_000)
        return hi + ' thousand' + (' ' + lo if lo else '')
    hi = _under_1000(n // 1_000_000)
    lo = _int_to_words(n % 1_000_000) if n % 1_000_000 else ''
    return hi + ' million' + (' ' + lo if lo else '')


def _dollars_to_words(text: str) -> str:
    def _replace(m):
        amount = int(m.group(1).replace(',', ''))
        return _int_to_words(amount) + ' dollars'
    return re.sub(r'\$([0-9,]+)', _replace, text)

def _spoken_bid(n: int) -> str:
    if n == 0:
        return 'zero'
    if n < 1_000:
        return _int_to_words(n)
    if n % 1_000 == 0:
        return _int_to_words(n // 1_000) + ' thousand'
    if n < 10_000:
        return _int_to_words(n // 100) + ' hundred'
    return _int_to_words(n)


def _short_bid(n: int) -> str:
    if n < 1_000:
        return _int_to_words(n)
    if n % 1_000 == 0:
        return _int_to_words(n // 1_000)
    return _int_to_words(n // 100)

# ---------------------------------------------------------------------------
# ElevenLabs TTS
# ---------------------------------------------------------------------------

def _load_tokens():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tokens.txt')
    tokens = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, _, v = line.partition('=')
                tokens[k.strip()] = v.strip()
    return tokens

_tokens = _load_tokens()
_EL_KEY = _tokens.get('elevenlabs-key', '')


def _load_voices():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'voices.txt')
    voices = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if ':' in line:
                    name, _, voice_id = line.partition(':')
                    name = name.strip()
                    voice_id = voice_id.strip()
                    if name and voice_id:
                        voices.append({'id': voice_id, 'name': name})
    except FileNotFoundError:
        pass
    return voices


_voices = _load_voices()

# Default to Auctioneer_Instant_Clone voice
_INSTANT_CLONE_NAME = 'Auctioneer_Instant_Clone'
_instant_clone = next(
    (v for v in _voices if v['name'].lower() == _INSTANT_CLONE_NAME.lower()),
    _voices[0] if _voices else {'id': '21m00Tcm4TlvDq8ikWAM', 'name': 'Rachel'}
)
_active_voice = {'id': _instant_clone['id'], 'name': _instant_clone['name']}


def _load_fillers():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fillers.txt')
    fillers = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    fillers.append(line)
    except FileNotFoundError:
        pass
    return fillers


_fillers = _load_fillers()
_last_filler: str = ''


def _load_templates():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'auction_templates.txt')
    sections = {}
    current_section = None
    current_block = []

    def _flush():
        text = '\n'.join(current_block).strip()
        if text and current_section:
            sections.setdefault(current_section, []).append(text)
        current_block.clear()

    try:
        with open(path) as f:
            for raw_line in f:
                line = raw_line.rstrip('\n')
                stripped = line.strip()
                if stripped.startswith('#'):
                    continue
                if stripped.startswith('[') and stripped.endswith(']'):
                    _flush()
                    current_section = stripped[1:-1]
                elif stripped == '':
                    _flush()
                else:
                    current_block.append(line)
            _flush()
    except FileNotFoundError:
        pass
    return sections


_templates = _load_templates()
_last_used  = {}


def _render_template(text, bidder_name=''):
    cb  = auction['current_bid']
    ask = auction['current_ask']
    name = bidder_name
    if not name and auction.get('leading_bidder'):
        b = next((b for b in BIDDERS if b['id'] == auction['leading_bidder']), None)
        if b:
            name = b['name']
    subs = {
        '{{vehicle}}':           auction['vehicle'],
        '{{current_bid}}':       _spoken_bid(cb),
        '{{next_bid}}':          _spoken_bid(ask),
        '{{short_current_bid}}': _short_bid(cb),
        '{{short_next_bid}}':    _short_bid(ask),
        '{{state}}':             name,
        '{{buyer_name}}':        name,
    }
    for var, val in subs.items():
        text = text.replace(var, val)
    return text


def _pick(section):
    pool = _templates.get(section, [])
    if not pool:
        return None
    avoid   = _last_used.get(section)
    choices = [t for t in pool if t != avoid] or pool
    text    = random.choice(choices)
    _last_used[section] = text
    return text


def _load_lots():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lots.txt')
    lots = []
    try:
        with open(path) as f:
            group = []
            for line in f:
                line = line.strip()
                if line.startswith('#'):
                    continue
                if line:
                    group.append(line)
                else:
                    if group:
                        lots.append({
                            'name':        group[0],
                            'declaration': group[1] if len(group) > 1 else '',
                            'highlight1':  group[2] if len(group) > 2 else '',
                            'highlight2':  group[3] if len(group) > 3 else '',
                            'video':       group[4] if len(group) > 4 else '',
                        })
                        group = []
            if group:
                lots.append({
                    'name':        group[0],
                    'declaration': group[1] if len(group) > 1 else '',
                    'highlight1':  group[2] if len(group) > 2 else '',
                    'highlight2':  group[3] if len(group) > 3 else '',
                    'video':       group[4] if len(group) > 4 else '',
                })
    except FileNotFoundError:
        pass
    return lots


_lots = _load_lots()

# TTS settings — defaults as specified
_tts_settings = {
    'model_id':          'eleven_flash_v2_5',
    'stability':         0.5,
    'similarity_boost':  0.75,
    'style':             0.20,
    'speed':             1.20,
    'use_speaker_boost': True,
}

# ---------------------------------------------------------------------------
# Speech & timing configuration
# ---------------------------------------------------------------------------

_SPEECH_DEFAULTS = {
    'filler_chain_max':        3,
    'filler_delay_ms':         0,
    'highlight_delay_seconds': 4,
    'min_bids_first':          2,
    'bids_between':            3,
    'timer_duration_seconds':  10,
}


def _load_speech_config():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'speech_config.txt')
    cfg = dict(_SPEECH_DEFAULTS)
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    k, _, v = line.partition('=')
                    k = k.strip()
                    v = v.split('#')[0].strip()
                    try:
                        cfg[k] = float(v) if '.' in v else int(v)
                    except ValueError:
                        pass
    except FileNotFoundError:
        pass
    return cfg


_speech_config = _load_speech_config()

EL_MODELS = [
    {'id': 'eleven_flash_v2_5',       'name': 'Eleven Flash v2.5 (fastest)'},
    {'id': 'eleven_flash_v2',         'name': 'Eleven Flash v2'},
    {'id': 'eleven_multilingual_v2',  'name': 'Eleven Multilingual v2'},
    {'id': 'eleven_turbo_v2_5',       'name': 'Eleven Turbo v2.5'},
    {'id': 'eleven_turbo_v2',         'name': 'Eleven Turbo v2'},
    {'id': 'eleven_monolingual_v1',   'name': 'Eleven English v1'},
]

# Request ID of the last successful TTS generation — passed to ElevenLabs
# on the next call so the model can match voice characteristics across clips.
_last_request_id: str = ''


def tts(text: str, speed: float = None):
    if not _EL_KEY:
        return None
    try:
        import httpx
        el = ElevenLabs(api_key=_EL_KEY, httpx_client=httpx.Client(verify=False))
        s = _tts_settings
        settings = VoiceSettings(
            stability=s['stability'],
            similarity_boost=s['similarity_boost'],
            style=s['style'],
            speed=speed if speed is not None else s['speed'],
            use_speaker_boost=s['use_speaker_boost'],
        )
        spoken = _dollars_to_words(text).replace('\n', ' ')

        audio = b''.join(el.text_to_speech.convert(
            voice_id=_active_voice['id'],
            text=spoken,
            model_id=s['model_id'],
            output_format='mp3_44100_128',
            voice_settings=settings,
        ))

        return base64.b64encode(audio).decode('utf-8')
    except Exception as e:
        print(f'[TTS error] {e}')
        return None

# ---------------------------------------------------------------------------
# Bidder definitions
# ---------------------------------------------------------------------------

BIDDERS = [
    {"id": "texas",      "name": "Texas",      "emoji": "🤠"},
    {"id": "california", "name": "California", "emoji": "🌴"},
    {"id": "vegas",      "name": "Las Vegas",  "emoji": "🎰"},
]

AUTO_BIDDER_IDS = ['texas', 'vegas']

# ---------------------------------------------------------------------------
# In-memory auction state
# ---------------------------------------------------------------------------

auction = {
    "status":                "idle",
    "vehicle":               "",
    "start_price":           0,
    "end_price":             0,
    "increment":             0,
    "current_bid":           0,
    "current_ask":           0,
    "leading_bidder":        None,
    "messages":              [],
    "bid_count":             0,
    "highlights_played":      0,
    "bids_since_highlight":   0,
    "filler_template_lines":  [],
    "events":                 [],
    "event_counter":          0,
}

# ---------------------------------------------------------------------------
# Auto-bidding state
# ---------------------------------------------------------------------------

_auction_lock   = threading.Lock()
_auto_bid_timer = None
_auto_budgets   = {}   # {'texas': max_bid, 'vegas': max_bid}


def _add_event(event_type, audio=None, bidder_id=None, bidder_name=None,
               amount=None, ask=None, ui_message=None, session_report=None):
    """Add an event to the queue for frontend polling."""
    _leading_b = next((b for b in BIDDERS if b['id'] == auction['leading_bidder']), None)
    auction['event_counter'] += 1
    auction['events'].append({
        'id':             auction['event_counter'],
        'type':           event_type,
        'audio':          audio,
        'bidder_id':      bidder_id,
        'bidder_name':    bidder_name,
        'amount':         amount,
        'ask':            ask,
        'ui_message':     ui_message,
        'session_report': session_report,
        'state': {
            'status':               auction['status'],
            'current_bid':          auction['current_bid'],
            'current_ask':          auction['current_ask'],
            'leading_bidder':       auction['leading_bidder'],
            'leading_name':         _leading_b['name'] if _leading_b else None,
            'bid_count':            auction['bid_count'],
            'highlights_played':    auction['highlights_played'],
            'bids_since_highlight': auction['bids_since_highlight'],
        },
    })
    # Cap queue size
    if len(auction['events']) > 100:
        auction['events'] = auction['events'][-100:]


def _cancel_auto_bid():
    """Cancel any pending auto-bid timer. Must be called with or without lock."""
    global _auto_bid_timer
    if _auto_bid_timer:
        _auto_bid_timer.cancel()
        _auto_bid_timer = None


def _schedule_auto_bid():
    """Schedule the next auto-bid after a random delay (2–9 seconds).
    Picks whichever auto-bidder can afford the ask and isn't currently leading."""
    global _auto_bid_timer
    _cancel_auto_bid()

    if auction['status'] != 'active':
        return

    ask = auction['current_ask']
    leader = auction['leading_bidder']

    # Only pick a bidder who is NOT the current leader and can afford the ask.
    # No self-bidding fallback — when both auto-bidders are exhausted the
    # auction pauses and the timer closes it on seller approval.
    candidates = [
        bid_id for bid_id in AUTO_BIDDER_IDS
        if ask <= _auto_budgets.get(bid_id, 0) and bid_id != leader
    ]

    if not candidates:
        # Auto-bidders maxed out — auction pauses; timer will close on seller approval
        return

    chosen = random.choice(candidates)
    delay  = random.uniform(1.5, 8.5)   # guaranteed within 10s window
    _auto_bid_timer = threading.Timer(delay, _do_auto_bid, args=[chosen])
    _auto_bid_timer.daemon = True
    _auto_bid_timer.start()


def _do_auto_bid(bidder_id: str):
    """Execute an auto-bid for Texas or Vegas. Runs in a background thread."""
    with _auction_lock:
        global _auto_bid_timer
        _auto_bid_timer = None

        if auction['status'] != 'active':
            return

        ask     = auction['current_ask']
        max_bid = _auto_budgets.get(bidder_id, 0)
        if ask > max_bid:
            # Budget exceeded — this auto-bidder is out
            _schedule_auto_bid()
            return

        bidder = next((b for b in BIDDERS if b['id'] == bidder_id), None)
        if not bidder:
            return

        # Occasionally jump by one increment (adds realism)
        amount = ask
        if random.random() < 0.2:
            jump = ask + auction['increment']
            if jump <= max_bid:
                amount = jump

        # Apply the bid
        auction['current_bid']            = amount
        auction['current_ask']            = amount + auction['increment']
        auction['leading_bidder']         = bidder_id
        auction['bid_count']             += 1
        auction['bids_since_highlight']  += 1
        auction['filler_template_lines']  = []

        add_msg('bid', f"{bidder['name']}: ${amount:,}", bidder=bidder_id)
        _log('BID', bidder=bidder['name'], amount=amount)

        session_report = None

        if amount >= auction['end_price']:
            auction['status'] = 'sold'
            audio_text = msg_sold(amount, bidder['name'])
            add_msg('auctioneer', audio_text)
            _log('CLOSING', audio_text)
            session_report = _generate_report('Reserve met — auto-bidder')
            _save_report(session_report, auction['vehicle'])
            audio = tts(audio_text)
            _add_event('sold', audio=audio, bidder_id=bidder_id,
                       bidder_name=bidder['name'], amount=amount,
                       ui_message=audio_text, session_report=session_report)
        else:
            audio_text = msg_bid(amount, bidder['name'], auction['current_ask'])
            ui_text    = msg_bid_ui(amount, bidder['name'], auction['current_ask'])
            add_msg('auctioneer', ui_text)
            _log('CALLOUT', audio_text)
            audio = tts(audio_text)
            _add_event('bid', audio=audio, bidder_id=bidder_id,
                       bidder_name=bidder['name'], amount=amount,
                       ask=auction['current_ask'], ui_message=ui_text)
            # Schedule the next auto-bid response
            _schedule_auto_bid()


def reset_state():
    global _auto_bid_timer, _auto_budgets
    _cancel_auto_bid()
    _auto_budgets = {}
    auction.update({
        "status": "idle", "vehicle": "", "start_price": 0,
        "end_price": 0, "increment": 0, "current_bid": 0,
        "current_ask": 0, "leading_bidder": None, "messages": [],
        "bid_count": 0, "highlights_played": 0, "bids_since_highlight": 0,
        "filler_template_lines": [], "events": [], "event_counter": 0,
    })


def add_msg(msg_type, text, bidder=None):
    auction["messages"].append({"type": msg_type, "text": text, "bidder": bidder})


def bid_amounts():
    ask = auction["current_ask"]
    inc = auction["increment"]
    return [ask, ask + inc, ask + 2 * inc]


# ---------------------------------------------------------------------------
# Auctioneer message templates
# ---------------------------------------------------------------------------

def msg_opening_bidcall(with_declaration=False):
    section = 'OPENING_CALLS_AFTER_DECLARATION' if with_declaration else 'OPENING_CALLS_NO_DECLARATION'
    tmpl = _pick(section)
    if tmpl:
        return _render_template(tmpl)
    s = auction["start_price"]
    if with_declaration:
        return f"Alright folks, let's get it started. Who's opening at {_spoken_bid(s)}?"
    return f"Next on the block — {auction['vehicle']}. Who's starting me at {_spoken_bid(s)}?"


def msg_bid(amount, location, next_ask):
    tmpl = _pick('BID_ACCEPTED')
    if tmpl:
        return _render_template(tmpl, bidder_name=location)
    return f"{_spoken_bid(amount)} to {location}. Looking for {_spoken_bid(next_ask)}."


def msg_bid_ui(amount, location, next_ask):
    tmpl = _pick('BID_ACCEPTED_UI')
    if tmpl:
        return _render_template(tmpl, bidder_name=location)
    return f"{_spoken_bid(amount)} from {location}. Looking for {_spoken_bid(next_ask)}."


def msg_sold(amount, location):
    tmpl = _pick('CLOSING')
    if tmpl:
        return _render_template(tmpl, bidder_name=location)
    return (
        f"Going once at {_spoken_bid(amount)}…..\n"
        f"Going twice…..\n"
        f"SOLD! {_spoken_bid(amount)} — {location} takes it!"
    )


def msg_force_close(amount, location):
    tmpl = _pick('CLOSING')
    if tmpl:
        return _render_template(tmpl, bidder_name=location)
    return (
        f"Last call at {_spoken_bid(amount)}…..\n"
        f"Going once….. Going twice…..\n"
        f"SOLD! {_spoken_bid(amount)} to {location}!"
    )


def msg_seller_approval(amount, location):
    return (
        f"{_spoken_bid(amount)} on the floor…..\n"
        f"Nobody else??…..\n"
        f"Hammer's up…..\n"
        f"SOLD on seller approval! {_spoken_bid(amount)} to {location}!"
    )


def msg_no_bids():
    return "No bids received. This vehicle will be passed."


# ---------------------------------------------------------------------------
# State helper
# ---------------------------------------------------------------------------

def get_state():
    leading = None
    if auction["leading_bidder"]:
        b = next((b for b in BIDDERS if b["id"] == auction["leading_bidder"]), None)
        if b:
            leading = b["name"]
    return {
        **auction,
        "events":        [],   # don't send full event list in state
        "bidders":       BIDDERS,
        "bid_amounts":   bid_amounts() if auction["status"] == "active" else [],
        "leading_name":  leading,
        "speech_config": _speech_config,
        "auto_budgets":  {k: v for k, v in _auto_budgets.items()},  # for debugging
    }


# ---------------------------------------------------------------------------
# Session logging & report generation
# ---------------------------------------------------------------------------

_session_log   = []
_session_start = None


def _log(event_type, text='', bidder=None, amount=None):
    _session_log.append({
        'time':   datetime.datetime.now().strftime('%H:%M:%S'),
        'type':   event_type,
        'text':   text,
        'bidder': bidder,
        'amount': amount,
    })


def _fmt_duration(start):
    if not start:
        return '—'
    s = int((datetime.datetime.now() - start).total_seconds())
    return f'{s // 60}m {s % 60}s'


def _truncate(text, n=80):
    text = text.replace('\n', ' ')
    return text[:n] + '…' if len(text) > n else text


def _generate_report(end_reason):
    if not _session_log:
        return None
    bids       = [e for e in _session_log if e['type'] == 'BID']
    n_fillers  = len([e for e in _session_log if e['type'] == 'FILLER'])
    n_hi       = len([e for e in _session_log if e['type'] == 'HIGHLIGHT'])
    final_bid  = f'${auction["current_bid"]:,}' if bids else 'No bids'
    winner     = next((b['name'] for b in BIDDERS if b['id'] == auction['leading_bidder']), '—')
    date_str   = datetime.datetime.now().strftime('%Y-%m-%d')
    start_time = _session_log[0]['time']
    end_time   = _session_log[-1]['time']
    duration   = _fmt_duration(_session_start)

    W = 62
    sep  = '═' * W
    dash = '─' * W

    def row(label, value):
        return f'  {label:<18}{value}'

    lines = [
        sep,
        '  AUCTION SESSION REPORT',
        sep,
        row('Vehicle',        auction['vehicle'] or '—'),
        row('Date',           date_str),
        row('Start',          start_time),
        row('End',            end_time),
        row('Duration',       duration),
        row('Closed by',      end_reason),
        dash,
        row('Total bids',     str(len(bids))),
        row('Opening price',  f'${auction["start_price"]:,}'),
        row('Reserve price',  f'${auction["end_price"]:,}'),
        row('Final bid',      final_bid),
        row('Winning bidder', winner),
        row('TX budget',      f'${_auto_budgets.get("texas", 0):,}'),
        row('LV budget',      f'${_auto_budgets.get("vegas", 0):,}'),
        row('Highlights',     str(n_hi)),
        row('Filler lines',   str(n_fillers)),
        sep,
        '  EVENT TIMELINE',
        dash,
    ]

    for e in _session_log:
        t    = e['time']
        kind = e['type']
        if kind == 'BID':
            reserve_tag = '  ★ RESERVE MET' if e['amount'] >= auction['end_price'] else ''
            lines.append(f'  [{t}]  BID         ${e["amount"]:,}  →  {e["bidder"]}{reserve_tag}')
        elif kind == 'CALLOUT':
            lines.append(f'  [{t}]  CALLOUT     {_truncate(e["text"])}')
        elif kind == 'FILLER':
            lines.append(f'  [{t}]  FILLER      {_truncate(e["text"])}')
        elif kind == 'HIGHLIGHT':
            lines.append(f'  [{t}]  HIGHLIGHT   {_truncate(e["text"])}')
        elif kind == 'OPENING':
            lines.append(f'  [{t}]  OPENING     {_truncate(e["text"])}')
        elif kind == 'DECLARATION':
            lines.append(f'  [{t}]  DECLARATION {_truncate(e["text"])}')
        elif kind == 'CLOSING':
            lines.append(f'  [{t}]  CLOSING     {_truncate(e["text"])}')

    lines.append(sep)
    return '\n'.join(lines)


def _save_report(report_text, vehicle):
    if not report_text:
        return None
    app_dir     = os.path.dirname(os.path.abspath(__file__))
    reports_dir = os.path.join(app_dir, 'session_reports')
    os.makedirs(reports_dir, exist_ok=True)
    ts        = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', vehicle or 'unknown')[:30]
    filename  = f'{ts}_{safe_name}.txt'
    with open(os.path.join(reports_dir, filename), 'w') as f:
        f.write(report_text)
    return filename


def _slim(text, audio):
    return {
        'slim':                 True,
        'audio':                audio,
        'msg':                  {'type': 'auctioneer', 'text': text},
        'status':               auction['status'],
        'highlights_played':    auction['highlights_played'],
        'bids_since_highlight': auction['bids_since_highlight'],
        'bid_count':            auction['bid_count'],
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")
    # return render_template("auction.html")


@app.route("/auction/start", methods=["POST"])
def start():
    global _session_start, _auto_budgets, _last_request_id
    with _auction_lock:
        _last_request_id = ''   # new lot — start fresh, no previous context
        _cancel_auto_bid()
        d = request.get_json()
        start_price  = int(d.get("start_price", 1000))
        increment    = int(d.get("increment",   200))
        end_price    = int(d.get("end_price",   6000))
        vehicle_name = d.get("vehicle", "Unknown Vehicle").strip()

        lot         = next((l for l in _lots if l['name'] == vehicle_name), None)
        declaration = lot['declaration'] if lot else ''
        lot_video   = lot['video']       if lot else ''

        # Declarations always on
        use_declaration = bool(declaration)
        _app_dir  = os.path.dirname(os.path.abspath(__file__))
        use_video = use_declaration and bool(lot_video) and os.path.exists(
            os.path.join(_app_dir, lot_video))

        auction.update({
            "status":                "active",
            "vehicle":               vehicle_name,
            "start_price":           start_price,
            "end_price":             end_price,
            "increment":             increment,
            "current_bid":           start_price,
            "current_ask":           start_price + increment,
            "leading_bidder":        None,
            "messages":              [],
            "bid_count":             0,
            "highlights_played":     0,
            "bids_since_highlight":  0,
            "filler_template_lines": [],
            "events":                [],
            "event_counter":         0,
        })

        # Both auto-bidders go at least 95 % of reserve, keeping bidding active
        # all the way up before stalling just below reserve.
        # Capped at end_price - increment so neither accidentally triggers SOLD.
        texas_max = max(
            start_price + increment * 2,
            min(int(end_price * random.uniform(0.95, 0.98)), end_price - increment)
        )
        vegas_max = max(
            start_price + increment * 2,
            min(int(end_price * random.uniform(0.95, 0.98)), end_price - increment)
        )
        _auto_budgets = {'texas': texas_max, 'vegas': vegas_max}
        print(f"[Auto-bid budgets] Texas: ${texas_max:,}  Vegas: ${vegas_max:,}  Reserve: ${end_price:,}")

        _session_log.clear()
        _session_start = datetime.datetime.now()

        audio             = None
        declaration_audio = None
        bidcall_audio     = None
        lot_video_url     = None

        opening_call = msg_opening_bidcall(with_declaration=use_declaration)

        if use_video:
            add_msg("auctioneer", declaration)
            add_msg("auctioneer", opening_call)
            bidcall_audio = tts(opening_call)
            lot_video_url = f'/videos/{lot_video}'
            _log('DECLARATION', declaration)
            _log('OPENING',     opening_call)
        elif use_declaration:
            add_msg("auctioneer", declaration)
            add_msg("auctioneer", opening_call)
            declaration_audio = tts(declaration, speed=1.0)
            bidcall_audio     = tts(opening_call)
            _log('DECLARATION', declaration)
            _log('OPENING',     opening_call)
        else:
            add_msg("auctioneer", opening_call)
            audio = tts(opening_call)
            _log('OPENING', opening_call)

        # Auto-bidding starts only after the frontend signals declaration is done
        # (via /auction/ready). Do NOT schedule here.

    return jsonify({
        **get_state(),
        "audio":             audio,
        "declaration_audio": declaration_audio,
        "bidcall_audio":     bidcall_audio,
        "lot_video_url":     lot_video_url,
    })


@app.route("/auction/ready", methods=["POST"])
def auction_ready():
    """Called by the frontend once declaration + opening audio have finished.
    Kicks off the first auto-bid so bids never interrupt the declaration."""
    with _auction_lock:
        if auction["status"] == "active":
            _schedule_auto_bid()
    return jsonify({"ok": True})


@app.route("/auction/bid", methods=["POST"])
def bid():
    """California's manual bid."""
    with _auction_lock:
        if auction["status"] != "active":
            return jsonify({"error": "Auction is not active"}), 400

        d      = request.get_json()
        bidder = next((b for b in BIDDERS if b["id"] == d.get("bidder_id")), None)
        amount = int(d.get("amount", 0))

        if not bidder:
            return jsonify({"error": "Unknown bidder"}), 400
        if amount < auction["current_ask"]:
            return jsonify({"error": f"Bid must be at least ${auction['current_ask']:,}"}), 400

        amount = min(amount, auction["end_price"])

        auction["current_bid"]            = amount
        auction["current_ask"]            = amount + auction["increment"]
        auction["leading_bidder"]         = bidder["id"]
        auction["bid_count"]             += 1
        auction["bids_since_highlight"]  += 1
        auction["filler_template_lines"]  = []

        add_msg("bid", f"{bidder['name']}: ${amount:,}", bidder=bidder["id"])
        _log('BID', bidder=bidder["name"], amount=amount)

        session_report = None

        if amount >= auction["end_price"]:
            auction["status"] = "sold"
            _cancel_auto_bid()
            audio_text = msg_sold(amount, bidder["name"])
            add_msg("auctioneer", audio_text)
            _log('CLOSING', audio_text)
            session_report = _generate_report('Reserve met — California')
            _save_report(session_report, auction['vehicle'])
        else:
            audio_text = msg_bid(amount, bidder["name"], auction["current_ask"])
            ui_text    = msg_bid_ui(amount, bidder["name"], auction["current_ask"])
            add_msg("auctioneer", ui_text)
            _log('CALLOUT', audio_text)
            # Reschedule auto-bid response
            _schedule_auto_bid()

        return jsonify({
            **get_state(),
            "audio":          tts(audio_text),
            "session_report": session_report,
        })


@app.route("/auction/events")
def events():
    """Poll for auto-bid events since a given event id."""
    since = int(request.args.get('since', 0))
    new_events = [e for e in auction['events'] if e['id'] > since]
    return jsonify({"events": new_events})


@app.route("/auction/close", methods=["POST"])
def force_close():
    with _auction_lock:
        if auction["status"] != "active":
            return jsonify({"error": "Auction is not active"}), 400

        _cancel_auto_bid()

        if not auction["leading_bidder"]:
            auction["status"] = "sold"
            spoken = msg_no_bids()
            end_reason = 'No bids'
        else:
            bidder = next(b for b in BIDDERS if b["id"] == auction["leading_bidder"])
            auction["status"] = "sold"
            below_reserve = auction["current_bid"] < auction["end_price"]
            if below_reserve:
                spoken = msg_seller_approval(auction["current_bid"], bidder["name"])
                end_reason = 'Sold on seller approval'
            else:
                spoken = msg_force_close(auction["current_bid"], bidder["name"])
                end_reason = 'Manual close'
        add_msg("auctioneer", spoken)
        _log('CLOSING', spoken)

        session_report = _generate_report(end_reason)
        _save_report(session_report, auction['vehicle'])

        return jsonify({**get_state(), "audio": tts(spoken), "session_report": session_report})


@app.route("/auction/filler", methods=["POST"])
def filler():
    if auction["status"] != "active":
        return jsonify({"skip": True})

    if not auction["filler_template_lines"]:
        tmpl = _pick('FILLERS')
        if not tmpl:
            return jsonify({"skip": True})
        lines = [l for l in tmpl.split('\n') if l.strip()]
        auction["filler_template_lines"] = lines

    if not auction["filler_template_lines"]:
        return jsonify({"skip": True})

    line = auction["filler_template_lines"].pop(0)
    text = _render_template(line)
    _log('FILLER', text)
    return jsonify({
        'slim':                 True,
        'filler_only':          True,
        'audio':                tts(text),
        'status':               auction['status'],
        'highlights_played':    auction['highlights_played'],
        'bids_since_highlight': auction['bids_since_highlight'],
        'bid_count':            auction['bid_count'],
    })


@app.route("/auction/highlight", methods=["POST"])
def highlight():
    if auction["status"] != "active":
        return jsonify({"skip": True})
    if auction["highlights_played"] >= 2:
        return jsonify({"skip": True})

    lot = next((l for l in _lots if l['name'] == auction['vehicle']), None)
    if not lot:
        return jsonify({"skip": True})

    hp  = auction["highlights_played"]
    bc  = auction["bid_count"]
    bsh = auction["bids_since_highlight"]
    min_bids_first = _speech_config['min_bids_first']
    bids_between   = _speech_config['bids_between']

    if hp == 0:
        if bc < min_bids_first:
            return jsonify({"skip": True})
        text = lot['highlight1']
    else:
        if bsh < bids_between:
            return jsonify({"skip": True})
        text = lot['highlight2']

    if not text:
        return jsonify({"skip": True})

    auction["highlights_played"]     += 1
    auction["bids_since_highlight"]   = 0
    auction["filler_template_lines"]  = []
    add_msg("auctioneer", text)
    _log('HIGHLIGHT', text)
    return jsonify(_slim(text, tts(text)))


@app.route("/speech-config", methods=["GET", "POST"])
def speech_config_route():
    if request.method == "POST":
        d = request.get_json() or {}
        for k, v in d.items():
            if k in _SPEECH_DEFAULTS:
                try:
                    _speech_config[k] = float(v) if '.' in str(v) else int(v)
                except (ValueError, TypeError):
                    pass
    return jsonify({"config": _speech_config})


@app.route("/reload-config", methods=["POST"])
def reload_config():
    _fillers.clear()
    _fillers.extend(_load_fillers())
    _templates.clear()
    _templates.update(_load_templates())
    _lots.clear()
    _lots.extend(_load_lots())
    _last_used.clear()
    new_cfg = _load_speech_config()
    _speech_config.update(new_cfg)
    return jsonify({"ok": True, "speech_config": _speech_config})


@app.route("/auction/reset", methods=["POST"])
def reset():
    with _auction_lock:
        had_activity = bool(_session_log)
        vehicle      = auction.get('vehicle', '')
        end_reason   = 'Reset' if auction['status'] != 'sold' else 'Reset after sold'
        session_report = _generate_report(end_reason) if had_activity else None
        if session_report:
            _save_report(session_report, vehicle)
        _session_log.clear()
        reset_state()
    return jsonify({**get_state(), "session_report": session_report})


@app.route("/session/reports")
def session_reports():
    reports_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'session_reports')
    if not os.path.exists(reports_dir):
        return jsonify({"reports": []})
    files = sorted(
        [f for f in os.listdir(reports_dir) if f.endswith('.txt')],
        reverse=True
    )
    return jsonify({"reports": files})


@app.route("/session/reports/<path:filename>")
def serve_report(filename):
    reports_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'session_reports')
    return send_from_directory(reports_dir, filename, mimetype='text/plain')


@app.route("/auction/state")
def state():
    return jsonify(get_state())


@app.route("/lots")
def lots():
    return jsonify({"lots": _lots})


@app.route("/voices")
def voices():
    return jsonify({"voices": _voices, "active": _active_voice, "models": EL_MODELS})


@app.route("/tts-settings", methods=["GET", "POST"])
def tts_settings():
    if request.method == "POST":
        d = request.get_json() or {}
        if "model_id"          in d: _tts_settings["model_id"]          = str(d["model_id"])
        if "stability"         in d: _tts_settings["stability"]         = max(0.0, min(1.0, float(d["stability"])))
        if "similarity_boost"  in d: _tts_settings["similarity_boost"]  = max(0.0, min(1.0, float(d["similarity_boost"])))
        if "style"             in d: _tts_settings["style"]             = max(0.0, min(1.0, float(d["style"])))
        if "speed"             in d: _tts_settings["speed"]             = max(0.7, min(1.2, float(d["speed"])))
        if "use_speaker_boost" in d: _tts_settings["use_speaker_boost"] = bool(d["use_speaker_boost"])
    return jsonify({"settings": _tts_settings, "models": EL_MODELS})


@app.route("/voice", methods=["POST"])
def set_voice():
    d = request.get_json()
    voice_id   = d.get("id", "").strip()
    voice_name = d.get("name", voice_id).strip()
    if not voice_id:
        return jsonify({"error": "voice id required"}), 400
    _active_voice["id"]   = voice_id
    _active_voice["name"] = voice_name
    return jsonify({"active": _active_voice})


@app.route("/videos/<path:filename>")
def serve_video(filename):
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), filename)


@app.route("/images/<path:filename>")
def serve_image(filename):
    images_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'images')
    return send_from_directory(images_dir, filename)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Starting Auction Simulator v8 at http://localhost:5006")
    app.run(debug=False, port=5006, threaded=True)
