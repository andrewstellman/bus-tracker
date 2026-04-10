#!/usr/bin/env python3
"""
MTA Bus Tracker — Real-time arrival predictions using the MTA Bus Time API.

Monitors configurable bus stops and tells you when to leave the house
based on walk time to each stop. All personal configuration (which stops,
walk times, dashboard title) lives in config.json — see config.example.json.

Usage:
    # CLI mode (one-shot):
    python bus_tracker.py

    # Web dashboard mode:
    python bus_tracker.py --web

    # Specify port:
    python bus_tracker.py --web --port 8080

Before running, get a free API key from:
    https://register.developer.obanyc.com/

Then either:
    1. Set the environment variable MTA_API_KEY
    2. Create a file called .api_key in this directory with your key
    3. Pass it with --key YOUR_KEY
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from urllib.error import URLError

# ── Configuration ──────────────────────────────────────────────────────────

SIRI_BASE = "https://bustime.mta.info/api/siri/stop-monitoring.json"
CONFIG_FILE = Path(__file__).parent / "config.json"

DEFAULT_CONFIG = {
    "title": "MTA Bus Tracker",
    "subtitle": "",
    "cushion_minutes": 0,
    "stops": {},
}


def load_config():
    """Load stop configuration from config.json."""
    if not CONFIG_FILE.exists():
        print(f"ERROR: {CONFIG_FILE} not found.\n")
        print("Copy config.example.json to config.json and edit it with your stops.")
        print("See README.md for details on finding MTA stop IDs.")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    merged = {**DEFAULT_CONFIG, **cfg}
    if not merged["stops"]:
        print("ERROR: No stops configured in config.json.")
        sys.exit(1)
    return merged


# ── API helpers ────────────────────────────────────────────────────────────

def load_api_key(cli_key=None):
    """Resolve the API key from CLI arg, env var, or .api_key file."""
    if cli_key:
        return cli_key
    key = os.environ.get("MTA_API_KEY")
    if key:
        return key
    key_file = Path(__file__).parent / ".api_key"
    if key_file.exists():
        return key_file.read_text().strip()
    return None


def fetch_stop_arrivals(api_key, stop_id, route_filter=None):
    """
    Call the SIRI StopMonitoring API and return a list of upcoming arrivals.

    Each arrival dict contains:
        - route: str (e.g. "B63")
        - destination: str
        - stops_away: int | None
        - distance_m: float | None
        - expected_arrival: datetime | None
        - minutes_away: float | None
        - stroller_accessible: bool
        - progress_status: str | None
        - vehicle_id: str | None
        - recorded_at: datetime | None
    """
    params = {
        "key": api_key,
        "MonitoringRef": stop_id,
        "version": "2",
        "StopMonitoringDetailLevel": "normal",
    }
    if route_filter:
        params["LineRef"] = route_filter

    url = f"{SIRI_BASE}?{urlencode(params)}"
    req = Request(url, headers={"Accept": "application/json"})

    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except (URLError, json.JSONDecodeError) as exc:
        return {"error": str(exc), "arrivals": []}

    delivery = (
        data.get("Siri", {})
        .get("ServiceDelivery", {})
        .get("StopMonitoringDelivery", [{}])[0]
    )

    visits = delivery.get("MonitoredStopVisit", [])
    arrivals = []

    for visit in visits:
        mvj = visit.get("MonitoredVehicleJourney", {})
        mc = mvj.get("MonitoredCall", {})

        # Parse expected arrival/departure time
        expected_str = (
            mc.get("ExpectedArrivalTime")
            or mc.get("ExpectedDepartureTime")
            or mc.get("AimedArrivalTime")
        )
        expected_dt = None
        minutes_away = None
        if expected_str:
            try:
                expected_dt = datetime.fromisoformat(expected_str)
                delta = expected_dt - datetime.now(timezone.utc)
                minutes_away = max(0, delta.total_seconds() / 60)
            except ValueError:
                pass

        # Distances
        distances = mc.get("Extensions", {}).get("Distances", {})

        route_name = mvj.get("PublishedLineName", [None])
        if isinstance(route_name, list):
            route_name = route_name[0] if route_name else "?"

        dest = mvj.get("DestinationName", [None])
        if isinstance(dest, list):
            dest = dest[0] if dest else "?"

        arrivals.append({
            "route": route_name,
            "destination": dest,
            "stops_away": distances.get("StopsFromCall"),
            "distance_m": distances.get("DistanceFromCall"),
            "expected_arrival": expected_dt,
            "minutes_away": minutes_away,
            "stroller_accessible": mc.get("Extensions", {}).get(
                "VehicleFeatures", {}).get("StrollerVehicle", False
            ),
            "progress_status": mvj.get("ProgressStatus"),
            "vehicle_id": mvj.get("VehicleRef"),
            "recorded_at": visit.get("RecordedAtTime"),
        })

    # Sort by minutes away (None → end)
    arrivals.sort(key=lambda a: a["minutes_away"] if a["minutes_away"] is not None else 9999)
    return {"error": None, "arrivals": arrivals}


def fetch_all_stops(api_key, stops, cushion_minutes=0):
    """Fetch arrivals for every configured stop. Returns a list of dicts."""
    results = []
    for label, cfg in stops.items():
        data = fetch_stop_arrivals(api_key, cfg["stop_id"], cfg.get("route_filter"))
        results.append({
            "label": label,
            "direction": cfg.get("direction", ""),
            "walk_minutes": cfg.get("walk_minutes", 5),
            "cushion_minutes": cushion_minutes,
            "error": data["error"],
            "arrivals": data["arrivals"],
        })
    return results


# ── CLI display ────────────────────────────────────────────────────────────

def format_arrival(arr, walk_minutes, cushion_minutes=0):
    """Format a single arrival line for the terminal."""
    mins = arr["minutes_away"]
    if mins is None:
        time_str = "time unknown"
    elif mins < 1:
        time_str = "arriving now"
    else:
        time_str = f"{mins:.0f} min away"

    stops = arr["stops_away"]
    stops_str = f" ({stops} stop{'s' if stops != 1 else ''} away)" if stops is not None else ""

    # How much time you have after walk + cushion
    hurry = ""
    if mins is not None:
        need = walk_minutes + cushion_minutes
        buffer = mins - need
        if buffer < 0:
            hurry = "  ✗ already too late"
        elif buffer < 2:
            hurry = "  ⚡ leave NOW"
        elif buffer < 5:
            hurry = f"  ⏳ leave in ~{buffer:.0f} min"
        else:
            hurry = f"  ✓ plenty of time ({buffer:.0f} min to spare)"

    return f"  {arr['route']:>4}  {time_str}{stops_str}{hurry}"


def print_dashboard(api_key, config):
    """Print a one-shot CLI dashboard."""
    now = datetime.now()
    title = config.get("title", "MTA Bus Tracker")
    subtitle = config.get("subtitle", "")
    cushion = config.get("cushion_minutes", 0)

    print(f"\n🚌 {title} — {now.strftime('%I:%M %p, %A %B %d')}")
    if subtitle:
        print(f"   {subtitle}")
    if cushion:
        print(f"   (includes +{cushion} min cushion)")
    print()

    results = fetch_all_stops(api_key, config["stops"], cushion)
    for stop in results:
        print(f"📍 {stop['label']}")
        direction_str = f"Direction: {stop['direction']}  •  " if stop["direction"] else ""
        print(f"   {direction_str}Walk: ~{stop['walk_minutes']} min")
        if stop["error"]:
            print(f"   ⚠ Error: {stop['error']}")
        elif not stop["arrivals"]:
            print("   No buses currently tracked on this route.")
        else:
            for arr in stop["arrivals"][:4]:  # show up to 4 upcoming buses
                print(format_arrival(arr, stop["walk_minutes"], cushion))
        print()


# ── Web dashboard ──────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{
    --bg: #1a1a2e; --card: #16213e; --accent: #0f3460;
    --text: #e8e8e8; --muted: #8b8b9e; --green: #4ecca3;
    --yellow: #f0c040; --red: #e74c3c; --blue: #5dade2;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg); color: var(--text); padding: 20px;
    max-width: 700px; margin: 0 auto; font-size: 18px;
  }}
  h1 {{ font-size: 1.8em; margin-bottom: 6px; }}
  .subtitle {{ color: var(--muted); font-size: 1.05em; margin-bottom: 20px; }}
  .updated {{ color: var(--muted); font-size: 0.9em; text-align: right; margin-bottom: 14px; }}
  .stop-card {{
    background: var(--card); border-radius: 12px; padding: 20px;
    margin-bottom: 14px; border-left: 4px solid var(--accent);
  }}
  .stop-label {{ font-weight: 600; font-size: 1.3em; margin-bottom: 4px; }}
  .stop-dir {{ color: var(--muted); font-size: 0.95em; margin-bottom: 12px; }}
  .arrival {{
    display: flex; justify-content: space-between; align-items: center;
    padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,0.05);
  }}
  .arrival:last-child {{ border-bottom: none; }}
  .route-badge {{
    background: var(--accent); color: var(--blue); font-weight: 700;
    padding: 4px 14px; border-radius: 6px; font-size: 1.15em; min-width: 50px;
    text-align: center;
  }}
  .arr-info {{ flex: 1; margin-left: 14px; }}
  .arr-time {{ font-size: 1.35em; font-weight: 600; }}
  .arr-stops {{ color: var(--muted); font-size: 0.95em; }}
  .arr-action {{ font-size: 1.05em; font-weight: 600; text-align: right; min-width: 130px; }}
  .action-go {{ color: var(--green); }}
  .action-soon {{ color: var(--yellow); }}
  .action-now {{ color: var(--red); font-weight: 700; }}
  .action-late {{ color: var(--muted); text-decoration: line-through; }}
  .no-buses {{ color: var(--muted); font-style: italic; padding: 8px 0; }}
  .error-msg {{ color: var(--red); padding: 8px 0; }}
  .refresh-btn {{
    display: block; width: 100%; padding: 14px; margin-top: 12px;
    background: var(--accent); color: var(--text); border: none;
    border-radius: 8px; font-size: 1.1em; cursor: pointer;
  }}
  .refresh-btn:active {{ opacity: 0.7; }}
</style>
</head>
<body>
<h1>{title_emoji} {title}</h1>
<div class="subtitle">{subtitle}</div>
<div class="updated" id="updated"></div>
<div id="stops"></div>
<button class="refresh-btn" onclick="refresh()">Refresh Now</button>

<script>
const REFRESH_MS = 30000;

async function refresh() {{
  document.getElementById('updated').textContent = 'Updating...';
  try {{
    const resp = await fetch('/api/arrivals');
    const data = await resp.json();
    render(data);
  }} catch(e) {{
    document.getElementById('updated').textContent = 'Error fetching data: ' + e;
  }}
}}

function render(data) {{
  const now = new Date();
  document.getElementById('updated').textContent =
    'Updated ' + now.toLocaleTimeString([], {{hour: '2-digit', minute: '2-digit', second: '2-digit'}});

  const container = document.getElementById('stops');
  container.innerHTML = '';

  for (const stop of data) {{
    const card = document.createElement('div');
    card.className = 'stop-card';

    let html = `<div class="stop-label">${{esc(stop.label)}}</div>`;
    const cushionNote = stop.cushion_minutes ? ` + ${{stop.cushion_minutes}} cushion` : '';
    html += `<div class="stop-dir">${{esc(stop.direction)}} · Walk ~${{stop.walk_minutes}} min${{cushionNote}}</div>`;

    if (stop.error) {{
      html += `<div class="error-msg">⚠ ${{esc(stop.error)}}</div>`;
    }} else if (!stop.arrivals || stop.arrivals.length === 0) {{
      html += `<div class="no-buses">No buses currently tracked</div>`;
    }} else {{
      for (const arr of stop.arrivals.slice(0, 4)) {{
        const mins = arr.minutes_away;
        let timeStr = 'Time unknown';
        let actionStr = '';
        let actionClass = 'action-go';

        if (mins !== null) {{
          if (mins < 1) {{ timeStr = 'Now'; }}
          else {{ timeStr = Math.round(mins) + ' min'; }}

          const need = stop.walk_minutes + (stop.cushion_minutes || 0);
          const buffer = mins - need;
          if (buffer < 0) {{ actionStr = 'Too late'; actionClass = 'action-late'; }}
          else if (buffer < 2) {{ actionStr = '⚡ Leave NOW'; actionClass = 'action-now'; }}
          else if (buffer < 5) {{ actionStr = 'Leave in ~' + Math.round(buffer) + ' min'; actionClass = 'action-soon'; }}
          else {{ actionStr = Math.round(buffer) + ' min to spare'; actionClass = 'action-go'; }}
        }}

        const stopsAway = arr.stops_away !== null ? arr.stops_away + ' stop' + (arr.stops_away !== 1 ? 's' : '') + ' away' : '';

        html += `<div class="arrival">
          <span class="route-badge">${{esc(arr.route || '?')}}</span>
          <div class="arr-info">
            <div class="arr-time">${{timeStr}}</div>
            <div class="arr-stops">${{stopsAway}}</div>
          </div>
          <div class="arr-action ${{actionClass}}">${{actionStr}}</div>
        </div>`;
      }}
    }}
    card.innerHTML = html;
    container.appendChild(card);
  }}
}}

function esc(s) {{
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}}

refresh();
setInterval(refresh, REFRESH_MS);
</script>
</body>
</html>"""


def make_handler(api_key, config):
    """Create an HTTP request handler with the API key and config baked in."""
    stops = config["stops"]
    cushion = config.get("cushion_minutes", 0)
    title = config.get("title", "MTA Bus Tracker")
    subtitle = config.get("subtitle", "")
    rendered_html = HTML_TEMPLATE.format(
        title=title,
        title_emoji="🚌",
        subtitle=subtitle,
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                if self.path == "/api/arrivals":
                    results = fetch_all_stops(api_key, stops, cushion)
                    # Serialize datetimes to ISO strings
                    for stop in results:
                        for arr in stop["arrivals"]:
                            if arr["expected_arrival"]:
                                arr["expected_arrival"] = arr["expected_arrival"].isoformat()
                    payload = json.dumps(results).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(payload)
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(rendered_html)
            except BrokenPipeError:
                pass  # Browser closed connection early — harmless

        def log_message(self, format, *args):
            pass

    return Handler


def run_web(api_key, config, port):
    """Start the web dashboard."""
    handler = make_handler(api_key, config)
    server = HTTPServer(("0.0.0.0", port), handler)
    print(f"🚌 {config.get('title', 'Bus Tracker')} running at http://localhost:{port}")
    print(f"   Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


# ── Entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="MTA Bus Tracker — real-time NYC bus arrival predictions"
    )
    parser.add_argument("--key", help="MTA Bus Time API key")
    parser.add_argument("--web", action="store_true", help="Start web dashboard")
    parser.add_argument("--port", type=int, default=5555, help="Web server port (default 5555)")
    parser.add_argument("--config", type=str, help="Path to config.json (default: ./config.json)")
    args = parser.parse_args()

    if args.config:
        global CONFIG_FILE
        CONFIG_FILE = Path(args.config)

    config = load_config()

    api_key = load_api_key(args.key)
    if not api_key:
        print("ERROR: No API key found.\n")
        print("Get a free key at: https://register.developer.obanyc.com/")
        print("\nThen do one of:")
        print("  1. export MTA_API_KEY=your_key_here")
        print("  2. echo 'your_key_here' > .api_key")
        print("  3. python bus_tracker.py --key your_key_here")
        sys.exit(1)

    if args.web:
        run_web(api_key, config, args.port)
    else:
        print_dashboard(api_key, config)


if __name__ == "__main__":
    main()
