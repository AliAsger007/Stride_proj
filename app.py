"""Stride Pro: public site + user module + admin module. Flask, SQLite, Stripe, SMTP."""
import os, re, smtplib, sqlite3, threading, secrets
from datetime import datetime, timedelta, date
from functools import wraps
from email.message import EmailMessage
from flask import Flask, g, jsonify, render_template, request, redirect, session, flash, abort
from werkzeug.security import generate_password_hash, check_password_hash
try:
    import stripe
except ImportError:
    stripe = None

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=14))
DB = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "stride_pro.db"))
BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:5000")
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "")
STRIPE_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
HOLD_MIN, HOURS = 15, [f"{h:02d}:00" for h in range(9, 18)]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LIVE = "('pending','confirmed','completed')"

def money(c): return "Free" if not c else f"${c/100:g}"
app.jinja_env.filters["money"] = money

# ---------- db ----------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB); g.db.row_factory = sqlite3.Row
        cut = (datetime.now() - timedelta(minutes=HOLD_MIN)).isoformat(timespec="seconds")
        g.db.execute("DELETE FROM bookings WHERE status='pending' AND created<?", (cut,)); g.db.commit()
    return g.db

@app.teardown_appcontext
def close(_):
    d = g.pop("db", None)
    if d: d.close()

def init_db():
    with sqlite3.connect(DB) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT UNIQUE, phone TEXT DEFAULT '',
          pw TEXT, role TEXT DEFAULT 'user', created TEXT);
        CREATE TABLE IF NOT EXISTS services(id TEXT PRIMARY KEY, name TEXT, dur TEXT, price_cents INTEGER DEFAULT 0,
          descr TEXT, featured INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT, email TEXT,
          service TEXT, day TEXT, slot TEXT, note TEXT, created TEXT, status TEXT DEFAULT 'confirmed');
        CREATE UNIQUE INDEX IF NOT EXISTS uq_slot ON bookings(day, slot) WHERE status IN ('pending','confirmed','completed');
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT, body TEXT,
          created TEXT, is_read INTEGER DEFAULT 0);""")
        if not c.execute("SELECT 1 FROM services").fetchone():
            c.executemany("INSERT INTO services VALUES(?,?,?,?,?,?,1)", [
                ("discovery", "Discovery Call", "20 min", 0, "Tell us your goals. We map a plan, no pressure.", 0),
                ("one-on-one", "1:1 Coaching", "60 min", 8000, "Fully personal sessions built around your body and schedule.", 1),
                ("mobility", "Mobility Reset", "45 min", 5500, "Undo desk-life stiffness with guided movement work.", 0)])
        em = os.environ.get("ADMIN_EMAIL", "admin@stride.local")
        if not c.execute("SELECT 1 FROM users WHERE email=?", (em,)).fetchone():
            c.execute("INSERT INTO users(name,email,pw,role,created) VALUES(?,?,?,?,?)",
                      ("Studio Admin", em, generate_password_hash(os.environ.get("ADMIN_PASSWORD", "admin123")),
                       "admin", datetime.now().isoformat(timespec="seconds")))

# ---------- email ----------
def _send(to, subject, body):
    host = os.environ.get("SMTP_HOST")
    if not host: print(f"[email not configured] To:{to} | {subject}\n{body}\n"); return
    try:
        m = EmailMessage(); m["From"] = os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", "no-reply@stride.local"))
        m["To"], m["Subject"] = to, subject; m.set_content(body)
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", 587)), timeout=15) as s:
            s.starttls(); s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"]); s.send_message(m)
    except Exception as e: print("Email failed:", e)

def send_mail(*a): threading.Thread(target=_send, args=a, daemon=True).start()

def notify(b):
    s = db().execute("SELECT name FROM services WHERE id=?", (b["service"],)).fetchone()
    sn = s["name"] if s else b["service"]
    send_mail(b["email"], f"Booking confirmed: {sn} on {b['day']}",
              f"Hi {b['name'].split()[0]},\n\nYou're booked for {sn} on {b['day']} at {b['slot']}.\nManage it in your account: {BASE_URL}/account\n\nStride")
    if OWNER_EMAIL: send_mail(OWNER_EMAIL, f"New booking: {b['name']}", f"{sn} | {b['day']} {b['slot']}\n{b['email']}\n{b.get('note','')}")

# ---------- security helpers ----------
@app.before_request
def load_user():
    g.user = None
    if "uid" in session:
        g.user = db().execute("SELECT * FROM users WHERE id=?", (session["uid"],)).fetchone()
        if not g.user: session.clear()
    if request.method == "POST" and not request.is_json and request.endpoint != "webhook":
        if not session.get("_t") or request.form.get("_t") != session["_t"]: abort(400, "Invalid form token")

@app.context_processor
def inject():
    def csrf():
        session.setdefault("_t", secrets.token_hex(16)); return session["_t"]
    return dict(user=g.get("user"), csrf=csrf)

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not g.user: flash("Please log in first."); return redirect("/login")
        return f(*a, **k)
    return w

def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if not g.user or g.user["role"] != "admin": abort(403)
        return f(*a, **k)
    return w

# ---------- public ----------
def active_services():
    return [dict(id=r["id"], name=r["name"], dur=r["dur"], price=money(r["price_cents"]), cents=r["price_cents"],
                 desc=r["descr"], featured=r["featured"]) for r in db().execute("SELECT * FROM services WHERE active=1 ORDER BY price_cents")]

@app.get("/")
def home():
    return render_template("index.html", services=active_services(), today=date.today().isoformat(),
                           maxd=(date.today() + timedelta(days=30)).isoformat())

@app.post("/contact")
def contact():
    n, e, b = (request.form.get(k, "").strip() for k in ("name", "email", "body"))
    if not (n and EMAIL_RE.match(e) and b): flash("Please fill all fields with a valid email.")
    else:
        db().execute("INSERT INTO messages(name,email,body,created) VALUES(?,?,?,?)", (n[:80], e, b[:2000], datetime.now().isoformat(timespec="seconds")))
        db().commit(); flash("Thanks! We'll reply soon.")
        if OWNER_EMAIL: send_mail(OWNER_EMAIL, f"New message from {n}", f"{e}\n\n{b}")
    return redirect("/#contact")

# ---------- user module ----------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        n, e, p = (request.form.get(k, "").strip() for k in ("name", "email", "password"))
        e = e.lower()
        if len(n) < 2 or not EMAIL_RE.match(e) or len(p) < 8: flash("Enter your name, a valid email and a password of 8+ characters.")
        else:
            try:
                cur = db().execute("INSERT INTO users(name,email,pw,created) VALUES(?,?,?,?)",
                                   (n[:80], e, generate_password_hash(p), datetime.now().isoformat(timespec="seconds")))
                db().commit(); session.clear(); session["uid"] = cur.lastrowid; session.permanent = True
                send_mail(e, "Welcome to Stride", f"Hi {n.split()[0]}, your account is ready. Book a session: {BASE_URL}/#book")
                return redirect("/account")
            except sqlite3.IntegrityError: flash("That email is already registered.")
    return render_template("auth.html", mode="register")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = db().execute("SELECT * FROM users WHERE email=?", (request.form.get("email", "").strip().lower(),)).fetchone()
        if u and check_password_hash(u["pw"], request.form.get("password", "")):
            session.clear(); session["uid"] = u["id"]; session.permanent = True
            return redirect("/admin" if u["role"] == "admin" else "/account")
        flash("Wrong email or password.")
    return render_template("auth.html", mode="login")

@app.get("/logout")
def logout(): session.clear(); return redirect("/")

@app.get("/account")
@login_required
def account():
    rows = db().execute("""SELECT b.*, s.name AS sname, s.price_cents FROM bookings b LEFT JOIN services s ON s.id=b.service
                           WHERE b.user_id=? ORDER BY b.day DESC, b.slot DESC""", (g.user["id"],)).fetchall()
    today = date.today().isoformat()
    up = [r for r in rows if r["day"] >= today and r["status"] in ("pending", "confirmed")]
    return render_template("account.html", upcoming=up, history=[r for r in rows if r not in up])

@app.post("/account/profile")
@login_required
def profile():
    n, ph = request.form.get("name", "").strip(), request.form.get("phone", "").strip()[:30]
    if len(n) < 2: flash("Name is too short.")
    else: db().execute("UPDATE users SET name=?, phone=? WHERE id=?", (n[:80], ph, g.user["id"])); db().commit(); flash("Profile updated.")
    return redirect("/account")

@app.post("/account/password")
@login_required
def password():
    old, new = request.form.get("old", ""), request.form.get("new", "")
    if not check_password_hash(g.user["pw"], old): flash("Current password is wrong.")
    elif len(new) < 8: flash("New password must be 8+ characters.")
    else: db().execute("UPDATE users SET pw=? WHERE id=?", (generate_password_hash(new), g.user["id"])); db().commit(); flash("Password changed.")
    return redirect("/account")

@app.post("/account/cancel/<int:bid>")
@login_required
def cancel(bid):
    b = db().execute("SELECT * FROM bookings WHERE id=? AND user_id=?", (bid, g.user["id"])).fetchone()
    if not b or b["status"] not in ("pending", "confirmed"): abort(404)
    if datetime.strptime(f"{b['day']} {b['slot']}", "%Y-%m-%d %H:%M") - datetime.now() < timedelta(hours=12):
        flash("Bookings can only be cancelled 12+ hours before the session. Please contact us.")
    else:
        db().execute("UPDATE bookings SET status='cancelled' WHERE id=?", (bid,)); db().commit()
        flash("Booking cancelled. If you paid online, we'll refund you manually."); 
        if OWNER_EMAIL: send_mail(OWNER_EMAIL, "Booking cancelled", f"{b['name']} cancelled {b['day']} {b['slot']}")
    return redirect("/account")

# ---------- booking API ----------
def valid_day(d): return date.today() <= d <= date.today() + timedelta(days=30) and d.weekday() != 6

@app.get("/api/slots")
def slots():
    try: d = datetime.strptime(request.args.get("date", ""), "%Y-%m-%d").date()
    except ValueError: return jsonify(error="Invalid date"), 400
    if not valid_day(d): return jsonify(slots=[])
    taken = {r["slot"] for r in db().execute(f"SELECT slot FROM bookings WHERE day=? AND status IN {LIVE}", (d.isoformat(),))}
    now = datetime.now()
    return jsonify(slots=[h for h in HOURS if h not in taken and not (d == now.date() and int(h[:2]) <= now.hour)])

@app.post("/api/book")
def book():
    if not g.user: return jsonify(error="Please log in or register to book."), 401
    j = request.get_json(silent=True) or {}
    s = db().execute("SELECT * FROM services WHERE id=? AND active=1", (j.get("service"),)).fetchone()
    slot, note = j.get("slot"), str(j.get("note", "")).strip()[:500]
    if not s: return jsonify(error="Choose a service."), 400
    if slot not in HOURS: return jsonify(error="Choose a time slot."), 400
    try: d = datetime.strptime(j.get("date") or "", "%Y-%m-%d").date()
    except ValueError: return jsonify(error="Choose a date."), 400
    if not valid_day(d): return jsonify(error="That date isn't available."), 400
    pay = s["price_cents"] > 0 and bool(STRIPE_KEY) and stripe is not None
    try:
        cur = db().execute("INSERT INTO bookings(user_id,name,email,service,day,slot,note,created,status) VALUES(?,?,?,?,?,?,?,?,?)",
            (g.user["id"], g.user["name"], g.user["email"], s["id"], d.isoformat(), slot, note,
             datetime.now().isoformat(timespec="seconds"), "pending" if pay else "confirmed")); db().commit()
    except sqlite3.IntegrityError: return jsonify(error="Sorry, that slot was just taken. Pick another."), 409
    if pay:
        try:
            stripe.api_key = STRIPE_KEY
            ss = stripe.checkout.Session.create(mode="payment", customer_email=g.user["email"],
                line_items=[{"quantity": 1, "price_data": {"currency": os.environ.get("CURRENCY", "usd"), "unit_amount": s["price_cents"],
                             "product_data": {"name": f"{s['name']} - {d} {slot}"}}}],
                metadata={"booking_id": str(cur.lastrowid)}, expires_at=int((datetime.now() + timedelta(minutes=30)).timestamp()),
                success_url=f"{BASE_URL}/account?paid=1", cancel_url=f"{BASE_URL}/?cancelled=1")
            return jsonify(ok=True, url=ss.url)
        except Exception as e:
            db().execute("DELETE FROM bookings WHERE id=?", (cur.lastrowid,)); db().commit(); print("Stripe error:", e)
            return jsonify(error="Payment couldn't start. Please try again."), 502
    notify(dict(name=g.user["name"], email=g.user["email"], service=s["id"], day=d.isoformat(), slot=slot, note=note))
    return jsonify(ok=True, message=f"You're booked, {g.user['name'].split()[0]}! {d:%a %d %b} at {slot}. See it in My Account.")

@app.post("/stripe/webhook")
def webhook():
    try: ev = stripe.Webhook.construct_event(request.data, request.headers.get("Stripe-Signature", ""), WEBHOOK_SECRET)
    except Exception: return "bad signature", 400
    obj = ev["data"]["object"]; bid = (obj.get("metadata") or {}).get("booking_id")
    if bid and ev["type"] == "checkout.session.completed":
        db().execute("UPDATE bookings SET status='confirmed' WHERE id=?", (bid,)); db().commit()
        b = db().execute("SELECT * FROM bookings WHERE id=?", (bid,)).fetchone()
        if b: notify(dict(b))
    elif bid and ev["type"] == "checkout.session.expired":
        db().execute("DELETE FROM bookings WHERE id=? AND status='pending'", (bid,)); db().commit()
    return "", 200

# ---------- admin module ----------
@app.get("/admin")
@app.get("/admin/<view>")
@admin_required
def admin(view="dash"):
    q, ctx = db().execute, {}
    if view == "dash":
        ctx = dict(users=q("SELECT COUNT(*) FROM users WHERE role='user'").fetchone()[0],
                   upcoming=q("SELECT COUNT(*) FROM bookings WHERE status='confirmed' AND day>=?", (date.today().isoformat(),)).fetchone()[0],
                   revenue=q("SELECT COALESCE(SUM(s.price_cents),0) FROM bookings b JOIN services s ON s.id=b.service WHERE b.status IN ('confirmed','completed')").fetchone()[0],
                   unread=q("SELECT COUNT(*) FROM messages WHERE is_read=0").fetchone()[0],
                   total=q("SELECT COUNT(*) FROM bookings").fetchone()[0],
                   next=q("SELECT * FROM bookings WHERE status='confirmed' AND day>=? ORDER BY day,slot LIMIT 6", (date.today().isoformat(),)).fetchall())
    elif view == "bookings":
        st = request.args.get("status", "")
        ctx = dict(rows=q("SELECT b.*, s.name AS sname FROM bookings b LEFT JOIN services s ON s.id=b.service" + (" WHERE b.status=?" if st else "") + " ORDER BY b.day DESC, b.slot", (st,) if st else ()).fetchall(), st=st)
    elif view == "services": ctx = dict(rows=q("SELECT * FROM services ORDER BY price_cents").fetchall())
    elif view == "users": ctx = dict(rows=q("SELECT u.*, (SELECT COUNT(*) FROM bookings WHERE user_id=u.id) AS nb FROM users u ORDER BY id DESC").fetchall())
    elif view == "messages": ctx = dict(rows=q("SELECT * FROM messages ORDER BY id DESC").fetchall())
    else: abort(404)
    return render_template("admin.html", view=view, **ctx)

@app.post("/admin/booking/<int:bid>")
@admin_required
def a_booking(bid):
    act = request.form.get("action")
    if act == "delete": db().execute("DELETE FROM bookings WHERE id=?", (bid,))
    elif act in ("confirmed", "completed", "cancelled"):
        try: db().execute("UPDATE bookings SET status=? WHERE id=?", (act, bid))
        except sqlite3.IntegrityError: flash("Another live booking already holds that slot.")
    db().commit(); return redirect(request.referrer or "/admin/bookings")

@app.post("/admin/service")
@admin_required
def a_service():
    f, name = request.form, request.form.get("name", "").strip()
    try: cents = int(round(float(f.get("price", 0)) * 100))
    except ValueError: cents = -1
    if not name or cents < 0: flash("Service needs a name and a valid price."); return redirect("/admin/services")
    sid = f.get("id") or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if f.get("action") == "delete":
        if db().execute("SELECT 1 FROM bookings WHERE service=?", (sid,)).fetchone():
            db().execute("UPDATE services SET active=0 WHERE id=?", (sid,)); flash("Service has bookings, so it was hidden instead of deleted.")
        else: db().execute("DELETE FROM services WHERE id=?", (sid,))
    else:
        db().execute("INSERT INTO services VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=?,dur=?,price_cents=?,descr=?,featured=?,active=?",
            (sid, name, f.get("dur", ""), cents, f.get("descr", ""), int("featured" in f), int("active" in f or not f.get("id"))) * 1 + (name, f.get("dur", ""), cents, f.get("descr", ""), int("featured" in f), int("active" in f or not f.get("id"))))
        flash("Service saved.")
    db().commit(); return redirect("/admin/services")

@app.post("/admin/user/<int:uid>")
@admin_required
def a_user(uid):
    if uid == g.user["id"]: flash("You can't change your own account here."); return redirect("/admin/users")
    act = request.form.get("action")
    if act == "delete": db().execute("DELETE FROM users WHERE id=?", (uid,)); db().execute("UPDATE bookings SET user_id=NULL WHERE user_id=?", (uid,))
    elif act in ("admin", "user"): db().execute("UPDATE users SET role=? WHERE id=?", (act, uid))
    db().commit(); return redirect("/admin/users")

@app.post("/admin/message/<int:mid>")
@admin_required
def a_message(mid):
    if request.form.get("action") == "delete": db().execute("DELETE FROM messages WHERE id=?", (mid,))
    else: db().execute("UPDATE messages SET is_read=1 WHERE id=?", (mid,))
    db().commit(); return redirect("/admin/messages")

init_db()
if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
