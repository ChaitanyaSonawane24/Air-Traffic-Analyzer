from flask import Flask, render_template, request, redirect, url_for, session, jsonify, g
import sqlite3
import os
import requests
import hashlib
from datetime import datetime
from functools import wraps


from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")  # change in production

DB_PATH = "airtraffic.db"

REGION_BOUNDS = {
    "min_lat": 5.0,
    "max_lat": 35.0,
    "min_lon": 68.0,
    "max_lon": 97.0
}

# ------------- DB helpers ------------- #

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(error=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    cur = db.cursor()

    # Users table (auth)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT DEFAULT 'user'
    )
    """)

    # Airports table (few sample airports, you can load full OpenFlights later)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS airports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        city TEXT,
        country TEXT,
        lat REAL,
        lon REAL
    )
    """)

    # Flight snapshots (for basic history / analytics)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS flight_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        icao24 TEXT,
        callsign TEXT,
        lat REAL,
        lon REAL,
        altitude REAL,
        velocity REAL,
        timestamp INTEGER
    )
    """)

    db.commit()

    # Create default admin if not exists
    cur.execute("SELECT * FROM users WHERE email = ?", ("admin@example.com",))
    if not cur.fetchone():
        pwd_hash = hash_password("admin123")
        cur.execute(
            "INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, ?)",
            ("Admin", "admin@example.com", pwd_hash, "admin")
        )
        db.commit()

    # Insert few sample airports if empty
    cur.execute("SELECT COUNT(*) as c FROM airports")
    if cur.fetchone()["c"] == 0:
        sample_airports = [
            ("BOM", "Chhatrapati Shivaji Maharaj Intl", "Mumbai", "India", 19.0896, 72.8656),
            ("DEL", "Indira Gandhi Intl", "Delhi", "India", 28.5562, 77.1000),
            ("BLR", "Kempegowda Intl", "Bengaluru", "India", 13.1989, 77.7063),
            ("HYD", "Rajiv Gandhi Intl", "Hyderabad", "India", 17.2403, 78.4294),
        ]
        cur.executemany("""
            INSERT INTO airports (code, name, city, country, lat, lon)
            VALUES (?, ?, ?, ?, ?, ?)
        """, sample_airports)
        db.commit()

def hash_password(pwd: str) -> str:
    return hashlib.sha256(pwd.encode()).hexdigest()

def check_password(pwd: str, hash_value: str) -> bool:
    return hash_password(pwd) == hash_value

# ------------- Auth decorator ------------- #

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped

# ------------- Views (pages) ------------- #

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT * FROM users WHERE email = ?", (email,))
        user = cur.fetchone()
        if user and check_password(password, user["password_hash"]):
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            return redirect(url_for("dashboard"))
        return render_template("login.html", error="Invalid credentials")

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")

@app.route("/analytics")
@login_required
def analytics():
    return render_template("analytics.html")

@app.route("/airports")
@login_required
def airports_page():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM airports ORDER BY city")
    airports = cur.fetchall()
    return render_template("airports.html", airports=airports)

@app.route("/routes")
@login_required
def routes_page():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM airports ORDER BY city")
    airports = cur.fetchall()
    return render_template("routes.html", airports=airports)

# ------------- External API helpers ------------- #

def fetch_opensky_states():
    url = "https://opensky-network.org/api/states/all"
    auth = None
    OPENSKY_USERNAME = os.getenv("OPENSKY_USERNAME")
    OPENSKY_PASSWORD = os.getenv("OPENSKY_PASSWORD")

    if OPENSKY_USERNAME and OPENSKY_PASSWORD:
        auth = (OPENSKY_USERNAME, OPENSKY_PASSWORD)

    resp = requests.get(url, auth=auth, timeout=10)
    resp.raise_for_status()
    return resp.json()

def fetch_openweather(lat, lon):
    OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

    if not OPENWEATHER_API_KEY:
        return None

    url = (
        f"https://api.openweathermap.org/data/2.5/weather"
        f"?lat={lat}&lon={lon}&appid={OPENWEATHER_API_KEY}&units=metric"
    )

    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()

# ------------- Core APIs ------------- #

@app.route("/api/flights/live")
@login_required
def api_flights_live():
    """Live flights in configured region with simple filters."""
    min_alt = float(request.args.get("min_alt", 0))
    max_alt = float(request.args.get("max_alt", 50000))
    callsign_search = request.args.get("callsign", "").strip().upper()

    bounds = REGION_BOUNDS

    data = fetch_opensky_states()
    states = data.get("states", []) or []

    flights = []
    ts = data.get("time", int(datetime.utcnow().timestamp()))
    for s in states:
        # OpenSky docs: [0]=icao24, [1]=callsign, [2]=origin_country, [3]=time_position,
        # [4]=last_contact, [5]=lon, [6]=lat, [7]=baro_altitude, [8]=on_ground,
        # [9]=velocity, [10]=heading, ...
        icao24 = s[0]
        callsign = (s[1] or "").strip()
        origin_country = s[2]
        lon = s[5]
        lat = s[6]
        altitude = s[7] or 0
        velocity = s[9] or 0
        heading = s[10] or 0

        if lat is None or lon is None:
            continue

        if not (bounds["min_lat"] <= lat <= bounds["max_lat"] and
                bounds["min_lon"] <= lon <= bounds["max_lon"]):
            continue

        if not (min_alt <= altitude <= max_alt):
            continue

        if callsign_search and callsign_search not in callsign:
            continue

        flights.append({
            "icao24": icao24,
            "callsign": callsign,
            "origin_country": origin_country,
            "lat": lat,
            "lon": lon,
            "altitude": altitude,
            "velocity": velocity,
            "heading": heading,
            "timestamp": ts
        })

    # (Optional) store a small snapshot sample for analytics
    db = get_db()
    cur = db.cursor()
    for f in flights[:100]:  # just limit to 100 for DB size
        cur.execute("""
            INSERT INTO flight_snapshots (icao24, callsign, lat, lon, altitude, velocity, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (f["icao24"], f["callsign"], f["lat"], f["lon"], f["altitude"], f["velocity"], f["timestamp"]))
    db.commit()

    return jsonify({"count": len(flights), "flights": flights})

@app.route("/api/stats/summary")
@login_required
def api_stats_summary():
    """Basic stats + simple congestion level from recent snapshots."""
    db = get_db()
    cur = db.cursor()

    # count last 10 minutes snapshots
    now = int(datetime.utcnow().timestamp())
    ten_min_ago = now - 600
    cur.execute("""
        SELECT COUNT(*) as c FROM flight_snapshots
        WHERE timestamp >= ?
    """, (ten_min_ago,))
    count_recent = cur.fetchone()["c"]

    # crude congestion thresholds (tune as you like)
    if count_recent < 50:
        congestion = "LOW"
    elif count_recent < 150:
        congestion = "MEDIUM"
    else:
        congestion = "HIGH"

    # flights per hour (approx) from snapshots
    cur.execute("""
        SELECT (timestamp/3600) as hour_bucket, COUNT(*) as c
        FROM flight_snapshots
        GROUP BY hour_bucket
        ORDER BY hour_bucket DESC
        LIMIT 24
    """)
    rows = cur.fetchall()
    flights_per_hour = [
        {"hour": int(r["hour_bucket"]), "count": r["c"]}
        for r in rows
    ]

    return jsonify({
        "recent_snapshot_count": count_recent,
        "congestion_level": congestion,
        "flights_per_hour": flights_per_hour
    })

@app.route("/api/airports")
@login_required
def api_airports():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM airports ORDER BY city")
    airports = [dict(r) for r in cur.fetchall()]
    return jsonify(airports)

@app.route("/api/airports/<code>/weather")
@login_required
def api_airport_weather(code):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM airports WHERE code = ?", (code.upper(),))
    ap = cur.fetchone()
    if not ap:
        return jsonify({"error": "Unknown airport"}), 404

    weather = fetch_openweather(ap["lat"], ap["lon"])
    if not weather:
        return jsonify({"error": "Weather API not configured"}), 500

    # Simple risk mapping
    main = weather["weather"][0]["main"].lower()
    wind = weather["wind"]["speed"]
    if "storm" in main or "thunder" in main or wind > 15:
        risk = "HIGH"
    elif "rain" in main or "fog" in main:
        risk = "MODERATE"
    else:
        risk = "LOW"

    return jsonify({
        "airport": ap["code"],
        "name": ap["name"],
        "city": ap["city"],
        "country": ap["country"],
        "temp": weather["main"]["temp"],
        "condition": weather["weather"][0]["description"],
        "wind_speed": wind,
        "risk": risk
    })

@app.route("/api/airports/<code>/traffic")
@login_required
def api_airport_traffic(code):
    """Approx traffic: flights within 100km of airport from current live data."""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM airports WHERE code = ?", (code.upper(),))
    ap = cur.fetchone()
    if not ap:
        return jsonify({"error": "Unknown airport"}), 404

    lat0, lon0 = ap["lat"], ap["lon"]

    # reuse live flights
    request.args = request.args  # no-op just for clarity
    data = fetch_opensky_states()
    states = data.get("states", []) or []

    def haversine(lat1, lon1, lat2, lon2):
        from math import radians, sin, cos, asin, sqrt
        R = 6371  # km
        dlat = radians(lat2 - lat1)
        dlon = radians(lon2 - lon1)
        a = sin(dlat/2)**2 + cos(radians(lat1))*cos(radians(lat2))*sin(dlon/2)**2
        c = 2 * asin(sqrt(a))
        return R * c

    arrivals = []
    departures = []
    others = []

    for s in states:
        callsign = (s[1] or "").strip()
        lon = s[5]
        lat = s[6]
        heading = s[10] or 0

        if lat is None or lon is None:
            continue

        dist = haversine(lat0, lon0, lat, lon)
        if dist <= 100:  # 100km radius
            info = {
                "callsign": callsign,
                "lat": lat,
                "lon": lon,
                "heading": heading,
                "distance_km": round(dist, 1)
            }
            # Very crude classification: if flying roughly towards airport lat/lon
            if dist > 30 and heading != 0 and heading != 360:
                arrivals.append(info)
            elif dist < 30:
                departures.append(info)
            else:
                others.append(info)

    return jsonify({
        "airport": ap["code"],
        "name": ap["name"],
        "arrivals": arrivals,
        "departures": departures,
        "others": others
    })

# Simple dummy route analytics (you can enhance using snapshots)
@app.route("/api/routes")
@login_required
def api_routes():
    origin = request.args.get("origin", "").upper()
    dest = request.args.get("dest", "").upper()
    if not origin or not dest:
        return jsonify({"error": "origin and dest required"}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM airports WHERE code = ?", (origin,))
    o = cur.fetchone()
    cur.execute("SELECT * FROM airports WHERE code = ?", (dest,))
    d = cur.fetchone()
    if not o or not d:
        return jsonify({"error": "Unknown origin/dest"}), 404

    # Very basic stats from snapshots: count flights near line between them etc.
    # For now, just return dummy stats.
    stats = {
        "origin": origin,
        "dest": dest,
        "avg_speed": 800,
        "avg_altitude": 35000,
        "estimated_daily_flights": 20
    }

    return jsonify({
        "route": stats,
        "origin_coords": {"lat": o["lat"], "lon": o["lon"]},
        "dest_coords": {"lat": d["lat"], "lon": d["lon"]}
    })

if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(debug=True)
