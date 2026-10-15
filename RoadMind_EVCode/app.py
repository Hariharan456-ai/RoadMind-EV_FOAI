"""RoadMind EV - EV trip planner that pre-computes route, charging and best meal stops at trip start.
Pure Python 3 (no pip installs). Live data: OSRM (routing), Nominatim (geocoding), Overpass/OpenStreetMap (restaurants, chargers).
Optional: set GOOGLE_API_KEY to rank food stops by real Google ratings."""
import json, math, os, re, threading, time, urllib.parse, urllib.request, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UA = {"User-Agent": "RoadMindEV-college-project/1.0"}
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
GKEY = os.environ.get("GOOGLE_API_KEY", "API KEY HERE")   # <- or paste your key here: os.environ.get("GOOGLE_API_KEY", "AIza...")

def http_json(url, data=None, timeout=40):
    req = urllib.request.Request(url, data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r: return json.loads(r.read())

def hav(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0]); dl = math.radians(b[1]-a[1]); dp = p2-p1
    h = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 12742*math.asin(math.sqrt(h))

# ---- meal profiles: ideal clock minute, dwell minutes, preferred OSM amenities / keywords ----
MEALS = [
 ("Early-morning tea", 360, 10, {"cafe", "fast_food"}, ["tea", "coffee", "chai", "bakery"]),
 ("Breakfast", 510, 25, {"restaurant", "cafe", "fast_food"}, ["breakfast", "tiffin", "idli", "dosa", "udupi", "bakery", "south_indian"]),
 ("Tea break", 660, 10, {"cafe", "fast_food"}, ["tea", "coffee", "chai", "bakery"]),
 ("Lunch", 780, 40, {"restaurant", "food_court"}, ["meals", "indian", "regional", "biryani", "thali", "south_indian", "vegetarian"]),
 ("Snacks break", 990, 15, {"cafe", "fast_food"}, ["snack", "bakery", "juice", "tea", "coffee", "sandwich"]),
 ("Dinner", 1230, 40, {"restaurant", "food_court"}, ["indian", "biryani", "tandoori", "regional", "meals", "chinese"]),
 ("Late-night tea", 1410, 10, {"cafe", "fast_food"}, ["tea", "coffee", "chai", "dhaba"]),
]
ICON = {"Early-morning tea": "tea", "Breakfast": "breakfast", "Tea break": "tea", "Lunch": "lunch", "Snacks break": "snack", "Dinner": "dinner", "Late-night tea": "night", "Charging": "charge"}

def traffic(clock):  # heuristic congestion multiplier by hour of day
    h = (clock // 60) % 24
    return 1.18 if 8 <= h < 10 else 1.22 if 17 <= h < 20 else 0.95 if (h >= 23 or h < 5) else 1.0

def fmt(m): m = int(round(m)) % 1440; return f"{m//60:02d}:{m%60:02d}"

def geocode(q):
    m = re.match(r"^\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*$", q)
    if m: return float(m[1]), float(m[2]), q
    r = http_json("https://nominatim.openstreetmap.org/search?format=json&limit=1&q=" + urllib.parse.quote(q))
    if not r: raise ValueError(f"Could not find place: {q}")
    return float(r[0]["lat"]), float(r[0]["lon"]), r[0]["display_name"].split(",")[0]

def get_route(o, d):
    u = f"https://router.project-osrm.org/route/v1/driving/{o[1]},{o[0]};{d[1]},{d[0]}?overview=full&geometries=geojson&alternatives=true"
    r = http_json(u)
    if r.get("code") != "Ok": raise ValueError("No driving route found")
    best = min(r["routes"], key=lambda x: x["duration"])      # shortest-time path among alternatives
    return best, len(r["routes"])

def resample(coords, step=1.0):
    pts = [(coords[0][1], coords[0][0], 0.0)]; acc = 0.0; nxt = step
    for i in range(1, len(coords)):
        a, b = (coords[i-1][1], coords[i-1][0]), (coords[i][1], coords[i][0]); seg = hav(a, b)
        while acc + seg >= nxt and seg > 0:
            t = (nxt-acc)/seg; pts.append((a[0]+(b[0]-a[0])*t, a[1]+(b[1]-a[1])*t, nxt)); nxt += step
        acc += seg
    pts.append((coords[-1][1], coords[-1][0], acc)); return pts

# ---- the planner: simulates the whole trip clock at trip start ----
def simulate(pts, base_speed, start, ev, skip_min):
    total = pts[-1][2]; clock = start; stops = []
    rng = ev["range"]; left = rng*ev["soc"]/100 if ev["on"] else 1e9; reserve = rng*0.10
    cpm = rng*ev["kw"]/ev["batt"]/60 if ev["on"] else 1          # km of range added per minute of charging
    meals = [(n, i+off, dw, am, kw) for off in (0, 1440, 2880) for (n, i, dw, am, kw) in MEALS if start+skip_min <= i+off <= start+2880]
    meals.sort(key=lambda m: m[1]); mi = 0; last = 0.0
    for k in range(1, len(pts)):
        seg = pts[k][2]-pts[k-1][2]; clock += seg/(base_speed/traffic(clock))*60; left -= seg; km = pts[k][2]
        while mi < len(meals) and meals[mi][1]+75 < clock: mi += 1     # window missed
        remain_km = total-km
        if remain_km/base_speed*60 < 20 or km-last < 20: continue
        meal = meals[mi] if mi < len(meals) and clock >= meals[mi][1]-10 else None
        need = ev["on"] and left <= reserve+40 and remain_km > left-reserve
        if need and not meal and mi < len(meals) and clock >= meals[mi][1]-45: meal = meals[mi]   # merge charge with nearby meal window
        opp = ev["on"] and meal and left < 0.5*rng and remain_km > left-reserve
        if not (meal or need): continue
        s = {"lat": pts[k][0], "lon": pts[k][1], "km": round(km, 1), "arrive": clock, "kind": meal[0] if meal else "Charging", "meal": bool(meal), "charge": None}
        dwell = meal[2] if meal else 10
        if need or opp:
            add = min(0.8*rng-left, remain_km+reserve+25-left); add = max(add, 15); mins = add/cpm
            s["charge"] = {"add_km": round(add), "minutes": round(mins), "soc_in": round(100*left/rng), "soc_out": round(100*min(left+add, rng)/rng)}
            dwell = max(dwell, mins); left += add
        if meal: mi += 1; s["want"] = meal[3:]
        s["ideal"] = meal[1] if meal else None
        clock += dwell; s["depart"] = clock; s["dwell"] = round(dwell); s["left_km"] = round(left); stops.append(s); last = km
    return stops, clock, left

def open_at(oh, minute):
    if not oh: return None
    if "24/7" in oh: return True
    rs = re.findall(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", oh)
    if not rs: return None
    m = minute % 1440
    for a, b, c, d in rs:
        s, e = int(a)*60+int(b), int(c)*60+int(d)
        if (s <= m <= e) or (e < s and (m >= s or m <= e)) or (s == e): return True
    return False

def overpass(lat, lon, tag, r):
    q = f'[out:json][timeout:25];nwr{tag}(around:{r},{lat},{lon});out center 90;'
    for u in OVERPASS:
        try: return http_json(u, ("data="+urllib.parse.quote(q)).encode(), 45).get("elements", [])
        except Exception: continue
    return []

def food_options(s):
    want_am, want_kw = s["want"]; out = []
    if GKEY:
        try:
            typ = "cafe" if "cafe" in want_am and "restaurant" not in want_am else "restaurant"
            r = http_json(f"https://maps.googleapis.com/maps/api/place/nearbysearch/json?location={s['lat']},{s['lon']}&radius=8000&type={typ}&key={GKEY}")
            print("GOOGLE:", r.get("status"), r.get("error_message", ""))
            for p in r.get("results", []):
                la, lo = p["geometry"]["location"]["lat"], p["geometry"]["location"]["lng"]; d = hav((s["lat"], s["lon"]), (la, lo))
                sc = p.get("rating", 3)*20 + math.log10(1+p.get("user_ratings_total", 0))*6 - d*3
                out.append({"name": p["name"], "lat": la, "lon": lo, "dist": round(d, 1), "score": sc, "rating": p.get("rating"), "reviews": p.get("user_ratings_total"), "info": p.get("vicinity", ""), "src": "Google"})
        except Exception: pass
    if not out:
        for rad in (5000, 12000, 25000):
            els = overpass(s["lat"], s["lon"], '["amenity"~"^(restaurant|cafe|fast_food|food_court)$"]["name"]', rad)
            if len(els) >= 6: break
        for e in els:
            t = e.get("tags", {}); la = e.get("lat") or e.get("center", {}).get("lat"); lo = e.get("lon") or e.get("center", {}).get("lon")
            if la is None: continue
            d = hav((s["lat"], s["lon"]), (la, lo)); eta = s["arrive"]; op = open_at(t.get("opening_hours"), eta)
            if op is False: continue
            txt = (t.get("cuisine", "")+" "+t.get("name", "")).lower()
            sc = 50 - d*3 + (14 if t.get("amenity") in want_am else 0) + 6*sum(k in txt for k in want_kw) + (8 if op else 0) + 4*("opening_hours" in t) + 3*("toilets" in t or "outdoor_seating" in t) + 3*(t.get("diet:vegetarian") in ("yes", "only")) + 2*("phone" in t or "website" in t)
            info = ", ".join(x for x in [t.get("cuisine", "").replace(";", "/"), "open at ETA" if op else ("hours unknown" if op is None else "")] if x)
            out.append({"name": t["name"], "lat": la, "lon": lo, "dist": round(d, 1), "score": sc, "rating": None, "info": info, "src": "OSM"})
    out.sort(key=lambda x: -x["score"]); return out[:5]

def charger_options(s):
    out = []
    for rad in (8000, 20000, 40000):
        els = overpass(s["lat"], s["lon"], '["amenity"="charging_station"]', rad)
        if els: break
    for e in els:
        t = e.get("tags", {}); la = e.get("lat") or e.get("center", {}).get("lat"); lo = e.get("lon") or e.get("center", {}).get("lon")
        if la is None: continue
        d = hav((s["lat"], s["lon"]), (la, lo)); kw = re.findall(r"(\d+)\s*kW", " ".join(v for k, v in t.items() if "output" in k))
        fast = max([int(x) for x in kw], default=0) >= 30 or "socket:ccs" in t or "socket:chademo" in t
        out.append({"name": t.get("name") or t.get("operator") or "EV charging station", "lat": la, "lon": lo, "dist": round(d, 1), "score": 40 - d*2 + (20 if fast else 0),
                    "info": ("DC fast" if fast else "AC/slow") + (f" - {t['operator']}" if t.get("operator") else "") + (f" - {t['capacity']} bays" if t.get("capacity") else ""), "src": "OSM"})
    out.sort(key=lambda x: -x["score"]); return out[:3]

def plan(a):
    o, d = geocode(a["origin"]), geocode(a["dest"])
    route, nalt = get_route(o, d); pts = resample(route["geometry"]["coordinates"])
    total = pts[-1][2]; base = total/(route["duration"]/3600); h, m = a["start"].split(":"); start = int(h)*60+int(m)
    ev = {"on": a["ev"], "range": float(a["range"]), "soc": float(a["soc"]), "kw": float(a["kw"]), "batt": float(a["batt"])}
    stops, clock, _ = simulate(pts, base, start, ev, int(a.get("skip", 45)))
    last_km = stops[-1]["km"] if stops else 0; last_t = stops[-1]["depart"] if stops else start
    t = last_t; k0 = next((i for i, p in enumerate(pts) if p[2] >= last_km), 0)
    for k in range(k0+1, len(pts)): t += (pts[k][2]-pts[k-1][2])/(base/traffic(t))*60
    if not stops: t = start + sum((pts[k][2]-pts[k-1][2])/(base/traffic(start)) * 60 for k in range(1, len(pts)))
    results = [None]*len(stops)
    def work(i):
        s = stops[i]
        food = food_options(s) if s["meal"] else []; ch = charger_options(s) if s["charge"] else []
        results[i] = {**{k: v for k, v in s.items() if k not in ("want",)}, "icon": ICON.get(s["kind"], "charge"), "arrive_s": fmt(s["arrive"]), "depart_s": fmt(s["depart"]), "ideal_s": fmt(s["ideal"]) if s["ideal"] is not None else None, "food": food, "chargers": ch}
    th = [threading.Thread(target=work, args=(i,)) for i in range(len(stops))]; [x.start() for x in th]; [x.join() for x in th]
    step = max(1, len(pts)//700)
    return {"origin": o[2], "dest": d[2], "origin_ll": o[:2], "dest_ll": d[:2], "km": round(total, 1), "drive_min": round(total/base*60), "alts": nalt,
            "start_s": fmt(start), "arrive_s": fmt(t), "days": int(t//1440), "total_min": round(t-start), "stops": results, "line": [[round(p[0], 5), round(p[1], 5)] for p in pts[::step]] + [[pts[-1][0], pts[-1][1]]]}

HERE = os.path.dirname(os.path.abspath(__file__))
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        raw = open(os.path.join(HERE, "index.html"), "rb").read()
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(raw)
    def do_POST(self):
        try: out = plan(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        except Exception as e: out = {"error": str(e)}
        raw = json.dumps(out).encode(); self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(raw)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000)); srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"\n  RoadMind EV running ->  http://localhost:{port}   (Ctrl+C to stop)\n")
    threading.Timer(1, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: pass