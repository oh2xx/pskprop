
import asyncio, json, math, os, threading, time
from collections import deque
from dataclasses import dataclass, asdict
from typing import Optional, Tuple, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
import paho.mqtt.client as mqtt

CONFIG_PATH = os.environ.get("PSKPROP_CONFIG", os.path.join(os.path.dirname(__file__), "config.json"))

def maidenhead_to_latlon(grid: str) -> Optional[Tuple[float, float]]:
    if not grid or len(grid) < 2:
        return None
    g = grid.strip().upper()
    if len(g) % 2 != 0:
        g += "MM"
    try:
        lon = (ord(g[0]) - ord('A')) * 20 - 180
        lat = (ord(g[1]) - ord('A')) * 10 - 90
        if len(g) >= 4:
            lon += int(g[2]) * 2
            lat += int(g[3]) * 1
        if len(g) >= 6:
            lon += (ord(g[4]) - ord('A')) * (5/60)
            lat += (ord(g[5]) - ord('A')) * (2.5/60)
        if len(g) >= 8:
            lon += int(g[6]) * (0.5/60)
            lat += int(g[7]) * (0.25/60)
        size_lon, size_lat = 20, 10
        if len(g) >= 4: size_lon, size_lat = 2, 1
        if len(g) >= 6: size_lon, size_lat = 5/60, 2.5/60
        if len(g) >= 8: size_lon, size_lat = 0.5/60, 0.25/60
        lon += size_lon/2; lat += size_lat/2
        return (lat, lon)
    except Exception:
        return None

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0088
    from math import radians, sin, cos, atan2, sqrt
    dphi = radians(lat2-lat1); dl = radians(lon2-lon1)
    a = sin(dphi/2)**2 + cos(radians(lat1))*cos(radians(lat2))*sin(dl/2)**2
    return 2*R*math.atan2(math.sqrt(a), math.sqrt(1-a))

BANDS = [
    ("160m", 1_800_000, 2_000_000),
    ("80m", 3_500_000, 4_000_000),
    ("60m", 5_000_000, 5_500_000),
    ("40m", 7_000_000, 7_300_000),
    ("30m", 10_100_000, 10_150_000),
    ("20m", 14_000_000, 14_350_000),
    ("17m", 18_068_000, 18_168_000),
    ("15m", 21_000_000, 21_450_000),
    ("12m", 24_890_000, 24_990_000),
    ("10m", 28_000_000, 29_700_000),
    ("6m", 50_000_000, 54_000_000),
]
def band_of_frequency(freq_hz: Optional[int]) -> Optional[str]:
    if freq_hz is None: return None
    try: f = int(freq_hz)
    except Exception: return None
    for name, lo, hi in BANDS:
        if lo <= f <= hi: return name
    return None

BAND_COLORS = {
    "160m":"#8B4513","80m":"#4B0082","40m":"#00008B","30m":"#008B8B",
    "20m":"#006400","17m":"#228B22","15m":"#8B8B00","12m":"#B8860B",
    "10m":"#B22222","6m":"#2F4F4F",
}

def load_config() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)

CONFIG = load_config()
home_latlon = maidenhead_to_latlon(CONFIG.get("home_locator","")) or (60.1708, 24.9375)
HOME_LAT, HOME_LON = home_latlon
RADIUS_KM = float(CONFIG.get("radius_km", 400))
AGE_MIN = int(CONFIG.get("age_minutes", 15))
ENABLED_BANDS = set(CONFIG.get("bands", [b[0] for b in BANDS]))
MAP_TYPE = CONFIG.get("map_type", "aeqd")
TOPICS = CONFIG.get("mqtt", {}).get("topics") or [f"pskr/filter/v2/{b}/#" for b in ENABLED_BANDS]

@dataclass
class Dot:
    lat: float; lon: float; band: str; snr: Optional[int]; ts: float; kind: str

DOTS: deque = deque(maxlen=20000)
APP_LOOP = None
MQTT_CLIENT = None
CURRENT_TOPICS = set()
PROCESSED = 0
SEEN = 0
DROP_COUNTS = {"grid_invalid":0,"missing_loc":0,"band_filtered":0,"radius":0,"parse":0}
RECENT: deque = deque(maxlen=50)

def _extract_fields(data: dict):
    sender_grid = data.get("senderLocator") or data.get("senderGrid") or data.get("sl")
    receiver_grid = data.get("receiverLocator") or data.get("receiverGrid") or data.get("rl")
    sender = data.get("senderCallsign") or data.get("sc")
    receiver = data.get("receiverCallsign") or data.get("rc")
    freq = data.get("frequency") or data.get("frequencyHz") or data.get("f")
    band = data.get("band") or data.get("b")
    snr = data.get("sNR");  snr = data.get("snr") if snr is None else snr;  snr = data.get("rp") if snr is None else snr
    ts = data.get("flowStartSeconds") or data.get("t")
    return sender_grid, receiver_grid, sender, receiver, freq, band, snr, ts

def normalize_band_str(b: str) -> str:
    if not b: return ""
    s = str(b).strip().lower()
    if s.endswith("m") and s[:-1].isdigit(): return s
    if s.isdigit(): return s + "m"
    if s.endswith("mhz"):
        try: float(s[:-3]); return ""
        except Exception: return ""
    return s

def band_label_from(freq, b):
    name = band_of_frequency(freq) if freq is not None else None
    if name: return name
    bs = normalize_band_str(b)
    if bs in {x[0] for x in BANDS}: return bs
    return None

def parse_snr(v):
    if v is None: return None
    try:
        if isinstance(v, (int,float)): return int(round(float(v)))
        return int(round(float(str(v).strip().replace("\u2212","-"))))
    except Exception:
        return None

def on_connect(client, userdata, flags, rc, properties=None):
    print(f"MQTT connected rc={rc}")
    for t in TOPICS:
        client.subscribe(t, qos=0)
        print(f"  subscribed: {t}")

def on_message(client, userdata, msg):
    global SEEN, PROCESSED
    try:
        data = json.loads(msg.payload.decode("utf-8"))
    except Exception:
        return
    SEEN += 1
    try:
        sender_grid, receiver_grid, sender, receiver, freq, band, snr, ts = _extract_fields(data)
        band = band_label_from(freq, band)
        if not band or band not in ENABLED_BANDS:
            DROP_COUNTS["band_filtered"] += 1; return
        if not sender_grid or not receiver_grid:
            DROP_COUNTS["missing_loc"] += 1; RECENT.append({"reason":"missing_loc","band":band}); return
        sl = maidenhead_to_latlon(sender_grid); rl = maidenhead_to_latlon(receiver_grid)
        if sl is None or rl is None:
            DROP_COUNTS["grid_invalid"] += 1; RECENT.append({"reason":"grid_invalid","band":band}); return
        now = time.time()
        if ts:
            try:
                tval = float(ts);  now = (tval/1000.0) if tval > 2_000_000_000 else tval
            except Exception: pass
        ds = haversine_km(HOME_LAT, HOME_LON, sl[0], sl[1])
        dr = haversine_km(HOME_LAT, HOME_LON, rl[0], rl[1])
        chosen = None; decision = None
        if dr <= RADIUS_KM:
            chosen=("sender", sl); decision="receiver_in_radius -> plot_sender"
        elif ds <= RADIUS_KM:
            chosen=("receiver", rl); decision="sender_in_radius -> plot_receiver"
        if chosen is None:
            DROP_COUNTS["radius"] += 1; RECENT.append({"reason":"radius","dS":round(ds,1),"dR":round(dr,1)}); return
        kind, (plat, plon) = chosen
        d = Dot(lat=plat, lon=plon, band=band, snr=parse_snr(snr), ts=now, kind=kind)
        DOTS.append(d); PROCESSED += 1
        RECENT.append({"reason":"ok","decision":decision,"band":band,"snr":parse_snr(snr)})
        payload = {"lat": d.lat, "lon": d.lon, "band": d.band, "snr": d.snr, "ts": d.ts, "kind": d.kind}
        if APP_LOOP is not None:
            asyncio.run_coroutine_threadsafe(hub.broadcast("add", payload), APP_LOOP)
    except Exception as e:
        DROP_COUNTS["parse"] += 1; RECENT.append({"reason":"exception","error":str(e)[:200]})

def mqtt_thread():
    global MQTT_CLIENT, CURRENT_TOPICS, TOPICS
    cfg = CONFIG.get("mqtt", {})
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cfg.get("client_id","pskprop"))
    MQTT_CLIENT = client
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(cfg.get("host","mqtt.pskreporter.info"), cfg.get("port",1883), cfg.get("keepalive",60))
    CURRENT_TOPICS = set(TOPICS)
    client.loop_forever(retry_first_connection=True)

def prune_thread():
    while True:
        cutoff = time.time() - AGE_MIN*60
        changed = False
        while DOTS and DOTS[0].ts < cutoff:
            DOTS.popleft(); changed = True
        if changed and APP_LOOP is not None:
            asyncio.run_coroutine_threadsafe(hub.broadcast("count", {"count": len(DOTS)}), APP_LOOP)
        time.sleep(10)

def _update_mqtt_subscriptions(new_bands):
    global TOPICS, CURRENT_TOPICS, MQTT_CLIENT
    if MQTT_CLIENT is None: return
    new_topics = set(f"pskr/filter/v2/{b}/#" for b in new_bands)
    for t in list(CURRENT_TOPICS - new_topics):
        try: MQTT_CLIENT.unsubscribe(t); print(f"  unsubscribed: {t}")
        except Exception: pass
    for t in list(new_topics - CURRENT_TOPICS):
        try: MQTT_CLIENT.subscribe(t, qos=0); print(f"  subscribed: {t}")
        except Exception: pass
    CURRENT_TOPICS = new_topics; TOPICS = list(new_topics)

class Hub:
    def __init__(self): self.clients: List[asyncio.Queue] = []
    async def connect(self) -> asyncio.Queue:
        q = asyncio.Queue(); self.clients.append(q); return q
    async def disconnect(self, q: asyncio.Queue):
        if q in self.clients: self.clients.remove(q)
    async def broadcast(self, event_type: str, payload: dict):
        msg = {"type": event_type, "payload": payload}
        for q in list(self.clients):
            try: await q.put(json.dumps(msg))
            except RuntimeError: pass

hub = Hub()

@asynccontextmanager
async def lifespan(app: FastAPI):
    global APP_LOOP
    APP_LOOP = asyncio.get_running_loop()
    t1 = threading.Thread(target=mqtt_thread, daemon=True); t1.start()
    t2 = threading.Thread(target=prune_thread, daemon=True); t2.start()
    try:
        yield
    finally:
        try:
            if MQTT_CLIENT is not None: MQTT_CLIENT.disconnect()
        except Exception:
            pass

app = FastAPI(title="PSK Prop Radius Map", lifespan=lifespan)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

@app.get("/main.js")
def legacy_main_js():
    return FileResponse(os.path.join(STATIC_DIR, "main.js"), media_type="application/javascript")

@app.get("/favicon.ico")
def legacy_favicon():
    fav = os.path.join(STATIC_DIR, "favicon.svg")
    if os.path.exists(fav):
        return FileResponse(fav, media_type="image/svg+xml")
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

@app.get("/config")
def get_config():
    public = {
        "home_locator": CONFIG.get("home_locator"),
        "home_latlon": [HOME_LAT, HOME_LON],
        "radius_km": RADIUS_KM,
        "age_minutes": AGE_MIN,
        "bands": list(ENABLED_BANDS),
        "band_colors": BAND_COLORS,
        "map_type": MAP_TYPE,
    }
    return JSONResponse(public)

@app.post("/config")
async def update_config(req: Request):
    body = await req.json()
    global CONFIG, HOME_LAT, HOME_LON, RADIUS_KM, AGE_MIN, ENABLED_BANDS, MAP_TYPE
    changed = False; bands_changed = False
    if "home_locator" in body:
        latlon = maidenhead_to_latlon(body["home_locator"])
        if latlon:
            CONFIG["home_locator"] = body["home_locator"]; HOME_LAT, HOME_LON = latlon; changed = True
    if "radius_km" in body:
        RADIUS_KM = float(body["radius_km"]); CONFIG["radius_km"] = RADIUS_KM; changed = True
    if "age_minutes" in body:
        AGE_MIN = int(body["age_minutes"]); CONFIG["age_minutes"] = AGE_MIN; changed = True
    if "bands" in body and isinstance(body["bands"], list):
        ENABLED_BANDS = set(body["bands"]); CONFIG["bands"] = list(ENABLED_BANDS); changed = True; bands_changed = True
    if "map_type" in body:
        MAP_TYPE = body["map_type"]; CONFIG["map_type"] = MAP_TYPE; changed = True
    if changed:
        DOTS.clear()
        if bands_changed: _update_mqtt_subscriptions(ENABLED_BANDS)
        if APP_LOOP is not None:
            try: asyncio.run_coroutine_threadsafe(hub.broadcast("snapshot", {"dots": []}), APP_LOOP)
            except Exception: pass
    return JSONResponse({"ok": True, "cleared": changed, "bands_changed": bands_changed})

@app.get("/events")
async def events(request: Request):
    q = await hub.connect()
    snap = [asdict(d) for d in list(DOTS)]
    await q.put(json.dumps({"type": "snapshot", "payload": {"dots": snap}}))
    async def event_generator():
        try:
            while True:
                if await request.is_disconnected(): break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield {"event": "message", "data": msg}
                except asyncio.TimeoutError:
                    yield {"event": "keepalive", "data": "{}"}
        finally:
            await hub.disconnect(q)
    return EventSourceResponse(event_generator())

@app.get("/stats")
def stats():
    return JSONResponse({
        "dots": len(DOTS),
        "processed": PROCESSED,
        "seen": SEEN,
        "enabled_bands": list(ENABLED_BANDS),
        "drops": DROP_COUNTS,
        "subscriptions": list(CURRENT_TOPICS),
    })

@app.get("/recent")
def recent():
    return JSONResponse({"recent": list(RECENT)})
