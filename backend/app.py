import os, re, time, json, hashlib, sqlite3
from urllib.parse import urlparse
from flask import Flask, request, jsonify
from flask_cors import CORS

try:
    import pymysql
except ImportError:  # only needed if you later switch to MySQL
    pymysql = None

app = Flask(__name__)
CORS(app, origins=os.environ.get("ALLOWED_ORIGIN", "*").split(","))

# Default = SQLite file (no setup needed). If DB_HOST is set, MySQL is used instead.
MYSQL = bool(os.environ.get("DB_HOST"))
SQLITE_PATH = os.environ.get("SQLITE_PATH", "database/fresherblr.db")
DAY_MS, TTL_DAYS = 86400000, 7
SALT = os.environ.get("SALT", "change-me")
TEXT = ["co", "role", "exp", "addr", "area", "apply", "name", "when", "cat", "mode"]
ALLOWED = TEXT + ["type", "park", "size", "contact", "link"]
BAD = re.compile(r"https?:|www\.|\.(com|in|xyz|link)\b|bit\.ly|t\.me|telegram|whatsapp group|(fee|deposit|advance|security amount|registration charge)|earn\s*(rs|₹|\d)|₹\s?\d|\brs\.?\s?\d", re.I)
PHONE = re.compile(r"^(\+91[\s-]?|0)?[6-9]\d{9}$")
EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")
SHORT = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "cutt.ly", "rb.gy", "is.gd", "ow.ly", "t.me", "wa.me",
         "telegram.me", "chat.whatsapp.com", "linktr.ee", "discord.gg"}


def link_error(u):
    if not u:
        return ""
    try:
        x = urlparse(u)
        h = (x.hostname or "").lower()
    except ValueError:
        return "Application link is not a valid web address."
    if x.scheme != "https" or "." not in h or re.fullmatch(r"[\d.]+", h) or x.username or x.password:
        return "Application link must be a full https:// address."
    if h.removeprefix("www.") in SHORT:
        return "Shortened and chat-group links are not allowed."
    if re.search(r"(fee|deposit|payment|registration-?charge)", (x.path or "") + "?" + (x.query or ""), re.I):
        return "Links that mention fees or payments are not allowed."
    return ""


hits = {}  # ip -> recent post times (keep ONE gunicorn worker so this stays accurate)
DUP = (sqlite3.IntegrityError,) + ((pymysql.err.IntegrityError,) if pymysql else ())


def connect():
    if MYSQL:
        kw = dict(host=os.environ["DB_HOST"], user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
                  database=os.environ["DB_NAME"], port=int(os.environ.get("DB_PORT", 3306)),
                  cursorclass=pymysql.cursors.DictCursor)
        if os.environ.get("DB_SSL"):
            kw["ssl"] = {"fake_flag_to_enable_tls": True}
        return pymysql.connect(**kw)
    os.makedirs(os.path.dirname(SQLITE_PATH) or ".", exist_ok=True)
    con = sqlite3.connect(SQLITE_PATH)
    con.row_factory = sqlite3.Row
    return con


def run(sql, args=(), fetch=False):
    con = connect()
    try:
        cur = con.cursor()
        cur.execute(sql.replace("?", "%s") if MYSQL else sql, args)
        rows = [dict(r) for r in cur.fetchall()] if fetch else None
        con.commit()
        return rows if fetch else (cur.lastrowid, cur.rowcount)
    finally:
        con.close()


def init():
    pk = "INT AUTO_INCREMENT PRIMARY KEY" if MYSQL else "INTEGER PRIMARY KEY AUTOINCREMENT"
    run(f"""CREATE TABLE IF NOT EXISTS posts(id {pk}, ts BIGINT NOT NULL, kind VARCHAR(10),
            flags INT DEFAULT 0, ok INT DEFAULT 0, why VARCHAR(100), owner CHAR(64), d TEXT NOT NULL)""")
    run("CREATE TABLE IF NOT EXISTS votes(post_id INT NOT NULL, ip CHAR(64) NOT NULL, PRIMARY KEY(post_id, ip))")


def ip():
    raw = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    return hashlib.sha256((SALT + raw).encode()).hexdigest()


def err(msg, code=400):
    return jsonify(error=msg), code


@app.get("/")
def health():
    return "Fresher BLR API is running (" + ("MySQL" if MYSQL else "SQLite") + ")"


@app.get("/api/posts")
def list_posts():
    now = int(time.time() * 1000)
    rows = run("SELECT id, ts, flags, ok, why, d FROM posts WHERE ts > ? OR kind='co' ORDER BY ts DESC LIMIT 500",
               (now - TTL_DAYS * DAY_MS,), fetch=True)
    for r in rows:
        r["d"] = json.loads(r["d"])
    return jsonify(posts=rows)


@app.post("/api/posts")
def create_post():
    body = request.get_json(silent=True) or {}
    d, token = body.get("d"), str(body.get("token", ""))
    if not isinstance(d, dict) or d.get("type") not in ("park", "other", "co") or len(token) < 16:
        return err("Invalid post.")
    if len(str(d.get("link", ""))) > 300:
        return err("Application link is too long.")
    d = {k: str(d[k]).strip()[:300] for k in ALLOWED if k in d}
    if d["type"] == "co":
        d.pop("link", None)
    e = link_error(d.get("link", ""))
    if e:
        return err(e)
    if len(d.get("co", "")) < 2:
        return err("Company name is required.")
    if BAD.search(" ".join(d.get(k, "") for k in TEXT)):
        return err("Links, payment or fee-related wording are not allowed.")
    if d["type"] != "co":
        c = d.get("contact", "")
        if not (EMAIL.match(c) or PHONE.match(re.sub(r"[\s-]", "", c))):
            return err("Enter a valid email or 10-digit Indian mobile number.")
    now = time.time()
    h = [t for t in hits.get(ip(), []) if now - t < 3600]
    if len(h) >= 5:
        return err("Too many posts from your network. Try again in an hour.", 429)
    hits[ip()] = h + [now]
    pid, _ = run("INSERT INTO posts(ts, kind, owner, d) VALUES(?,?,?,?)",
                 (int(now * 1000), d["type"], hashlib.sha256(token.encode()).hexdigest(), json.dumps(d)))
    return jsonify(id=pid), 201


@app.post("/api/posts/<int:pid>/vote")
def vote(pid):
    b = request.get_json(silent=True) or {}
    kind = b.get("kind")
    if kind not in ("flags", "ok"):
        return err("Invalid vote.")
    try:
        w = 1 if kind == "ok" else max(1, min(3, int(b.get("w", 1))))
    except (TypeError, ValueError):
        return err("Invalid vote.")
    try:
        run("INSERT INTO votes(post_id, ip) VALUES(?,?)", (pid, ip()))
    except DUP:
        return err("You have already voted on this post.", 409)
    if kind == "flags":
        run("UPDATE posts SET flags=flags+?, why=COALESCE(?, why) WHERE id=?", (w, str(b.get("why", ""))[:100] or None, pid))
    else:
        run("UPDATE posts SET ok=ok+1 WHERE id=?", (pid,))
    return jsonify(ok=True)


@app.delete("/api/posts/<int:pid>")
def remove(pid):
    tok = hashlib.sha256(request.headers.get("X-Token", "").encode()).hexdigest()
    _, n = run("DELETE FROM posts WHERE id=? AND owner=?", (pid, tok))
    return (jsonify(ok=True), 200) if n else err("Not allowed.", 403)


try:
    init()
except Exception as e:
    print("DB init failed:", e)

if __name__ == "__main__":
    app.run(debug=True, port=5000)