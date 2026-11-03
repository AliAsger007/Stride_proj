"""Stride Pro: public site + user module + admin module. Flask, SQLite, Stripe, SMTP."""
import os, re, smtplib, sqlite3, threading, secrets
from datetime import datetime, timedelta, date
from functools import wraps
from email.message import EmailMessage
from flask import Flask, g, jsonify, render_template, request, redirect, session, flash, abort
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
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
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "static", "uploads")
HOLD_MIN, HOURS = 15, [f"{h:02d}:00" for h in range(9, 18)]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LIVE = "('pending','confirmed','completed')"

def money(c): return "Free" if not c else f"₹{c/100:g}"
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
          created TEXT, is_read INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS site_content(key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS achievements(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
          description TEXT DEFAULT '', year TEXT DEFAULT '', sort_order INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS trainers(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, image TEXT DEFAULT '', bio TEXT DEFAULT '',
          specialization TEXT DEFAULT '', certifications TEXT DEFAULT '', experience TEXT DEFAULT '', programs TEXT DEFAULT '', availability TEXT DEFAULT '', created TEXT);
        CREATE TABLE IF NOT EXISTS membership_plans(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, duration_days INTEGER NOT NULL DEFAULT 30,
          duration_label TEXT DEFAULT '', description TEXT DEFAULT '', benefits TEXT DEFAULT '', included_sessions TEXT DEFAULT '', price_cents INTEGER NOT NULL DEFAULT 0,
          active INTEGER DEFAULT 1, created TEXT);
        CREATE TABLE IF NOT EXISTS membership_discounts(id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL, kind TEXT NOT NULL DEFAULT 'percent',
          value REAL NOT NULL DEFAULT 0, starts TEXT DEFAULT '', expires TEXT DEFAULT '', active INTEGER DEFAULT 1, created TEXT,
          FOREIGN KEY(plan_id) REFERENCES membership_plans(id));
        CREATE TABLE IF NOT EXISTS memberships(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, plan_id INTEGER NOT NULL,
          discount_id INTEGER, original_price_cents INTEGER NOT NULL, paid_price_cents INTEGER NOT NULL, payment_status TEXT DEFAULT 'pending',
          status TEXT DEFAULT 'pending', start_date TEXT, expiry_date TEXT, stripe_session_id TEXT, created TEXT,
          FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(plan_id) REFERENCES membership_plans(id));""" )
        message_columns = {r[1] for r in c.execute("PRAGMA table_info(messages)")}
        for column, definition in (("user_id", "INTEGER"), ("subject", "TEXT DEFAULT ''"), ("recipient_type", "TEXT DEFAULT 'owner'"), ("trainer_id", "INTEGER")):
            if column not in message_columns: c.execute(f"ALTER TABLE messages ADD COLUMN {column} {definition}")
        defaults = {
            "gym_name": "Stride Pro", "gym_description": "Personal coaching and mobility sessions for busy people who want to move better and feel stronger.",
            "gym_facilities": "Personal training\nMobility studio\nRecovery and movement assessments", "gym_address": "Add your studio address",
            "gym_map_url": "", "gym_hours": "Mon–Sat · 9:00 AM–6:00 PM", "gym_phone": "", "gym_email": OWNER_EMAIL, "gym_social": "",
            "owner_name": "Ali", "owner_image": "", "owner_bio": "Stride Pro is built around practical coaching, thoughtful programming, and long-term progress.",
            "client_retention": "98%", "client_experience": "4.9/5"
        }
        for key, value in defaults.items(): c.execute("INSERT OR IGNORE INTO site_content(key,value) VALUES(?,?)", (key, value))
        if not c.execute("SELECT 1 FROM services").fetchone():
            c.executemany("INSERT INTO services VALUES(?,?,?,?,?,?,1)", [
                ("discovery", "Discovery Call", "20 min", 0, "Tell us your goals. We map a plan, no pressure.", 0),
                ("one-on-one", "1:1 Coaching", "60 min", 8000, "Fully personal sessions built around your body and schedule.", 1),
                ("mobility", "Mobility Reset", "45 min", 5500, "Undo desk-life stiffness with guided movement work.", 0)])
        if not c.execute("SELECT 1 FROM membership_plans").fetchone():
            c.executemany("INSERT INTO membership_plans(name,duration_days,duration_label,description,benefits,included_sessions,price_cents,active,created) VALUES(?,?,?,?,?,?,?,?,?)", [
                ("Starter", 30, "Monthly", "A simple way to build a consistent training rhythm.", "Gym access\nMovement assessment", "2", 5000, 1, datetime.now().isoformat(timespec="seconds")),
                ("Momentum", 90, "3 months", "More time, more momentum, more measurable progress.", "Gym access\nMonthly progress review\nPriority booking", "6", 13500, 1, datetime.now().isoformat(timespec="seconds")),
                ("Unlimited", 365, "Annual", "The complete Stride Pro experience for your strongest year.", "Unlimited gym access\nQuarterly review\nPriority booking", "24", 48000, 1, datetime.now().isoformat(timespec="seconds"))])
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

def active_discount(plan_id, on=None):
    on = on or date.today().isoformat()
    return db().execute("SELECT * FROM membership_discounts WHERE plan_id=? AND active=1 AND (starts='' OR starts<=?) AND (expires='' OR expires>=?) ORDER BY id DESC LIMIT 1", (plan_id, on, on)).fetchone()

def membership_price(plan, discount):
    original = int(plan["price_cents"] or 0)
    if not discount: return original, None
    if discount["kind"] == "percent":
        price = round(original * (1 - float(discount["value"]) / 100))
    else: price = int(round(float(discount["value"])))
    return max(0, min(original, price)), discount

def membership_offers():
    offers=[]
    for plan in db().execute("SELECT * FROM membership_plans WHERE active=1 ORDER BY price_cents, id").fetchall():
        discount=active_discount(plan["id"]); final, discount=membership_price(plan, discount)
        offers.append(dict(plan=plan, discount=discount, original=plan["price_cents"], final=final,
                           benefits=[x.strip() for x in (plan["benefits"] or "").splitlines() if x.strip()]))
    return offers

# ---------- public ----------
def active_services():
    return [dict(id=r["id"], name=r["name"], dur=r["dur"], price=money(r["price_cents"]), cents=r["price_cents"],
                 desc=r["descr"], featured=r["featured"]) for r in db().execute("SELECT * FROM services WHERE active=1 ORDER BY price_cents")]
def public_content():
    content = {r["key"]: r["value"] for r in db().execute("SELECT key,value FROM site_content")}
    content["facilities_list"] = [x.strip() for x in content.get("gym_facilities", "").splitlines() if x.strip()]
    content["social_list"] = [x.strip() for x in content.get("gym_social", "").splitlines() if x.strip()]
    content["sessions_delivered"] = db().execute("SELECT COUNT(*) FROM bookings WHERE status='completed'").fetchone()[0]
    return content
def trainer_rows():
    return db().execute("SELECT * FROM trainers ORDER BY name").fetchall()

def save_uploaded_image(file):
    if not file or not file.filename: return ""
    ext = os.path.splitext(secure_filename(file.filename))[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"): return ""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    name = secrets.token_hex(8) + ext
    file.save(os.path.join(UPLOAD_DIR, name))
    return "/static/uploads/" + name

@app.get("/")
def home():
    return render_template("index.html", services=active_services(), today=date.today().isoformat(),
                           maxd=(date.today() + timedelta(days=30)).isoformat(), content=public_content(),
                           achievements=db().execute("SELECT * FROM achievements ORDER BY sort_order, id").fetchall(),
                           trainers=trainer_rows() if g.user else [])

@app.post("/contact")
def contact():
    n, e, b = (request.form.get(k, "").strip() for k in ("name", "email", "body"))
    if not (n and EMAIL_RE.match(e) and b): flash("Please fill all fields with a valid email.")
    else:
        db().execute("INSERT INTO messages(name,email,body,created) VALUES(?,?,?,?)", (n[:80], e, b[:2000], datetime.now().isoformat(timespec="seconds")))
        db().commit(); flash("Thanks! We'll reply soon.")
        if OWNER_EMAIL: send_mail(OWNER_EMAIL, f"New message from {n}", f"{e}\n\n{b}")
    return redirect("/#contact")

@app.post("/contact/trainer")
@login_required
def contact_trainer():
    recipient = request.form.get("recipient", "owner")
    trainer_id = request.form.get("trainer_id") if recipient == "trainer" else None
    trainer = db().execute("SELECT * FROM trainers WHERE id=?", (trainer_id,)).fetchone() if trainer_id else None
    if recipient == "trainer" and not trainer: flash("Please choose a valid trainer."); return redirect("/#trainers")
    subject = request.form.get("subject", "").strip()[:160]
    body = request.form.get("body", "").strip()[:2000]
    if not subject or not body: flash("Please add a subject and message."); return redirect("/#trainers")
    db().execute("INSERT INTO messages(name,email,body,created,is_read,user_id,subject,recipient_type,trainer_id) VALUES(?,?,?,?,0,?,?,?,?)",
                 (g.user["name"], g.user["email"], body, datetime.now().isoformat(timespec="seconds"), g.user["id"], subject, recipient, trainer_id))
    db().commit(); flash("Your message was sent successfully.")
    target = trainer["name"] if trainer else "the gym owner"
    if trainer and trainer["email"] if "email" in trainer.keys() else False: send_mail(trainer["email"], subject, body)
    elif OWNER_EMAIL: send_mail(OWNER_EMAIL, f"{subject} — {g.user['name']}", body)
    return redirect("/#trainers")

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

@app.get("/membership")
@login_required
def membership():
    db().execute("UPDATE memberships SET status='expired' WHERE user_id=? AND status='active' AND expiry_date<?", (g.user["id"], date.today().isoformat())); db().commit()
    purchases = db().execute("SELECT m.*, p.name AS plan_name, p.duration_label FROM memberships m JOIN membership_plans p ON p.id=m.plan_id WHERE m.user_id=? ORDER BY m.id DESC", (g.user["id"],)).fetchall()
    return render_template("membership.html", offers=membership_offers(), purchases=purchases)

@app.post("/membership/purchase/<int:plan_id>")
@login_required
def purchase_membership(plan_id):
    plan=db().execute("SELECT * FROM membership_plans WHERE id=? AND active=1", (plan_id,)).fetchone()
    if not plan: flash("That membership plan is no longer available."); return redirect("/membership")
    if not STRIPE_KEY or stripe is None:
        flash("Online membership payment is not configured yet. No purchase was recorded."); return redirect("/membership")
    final, discount=membership_price(plan, active_discount(plan_id))
    if final < 0: flash("This membership price is invalid."); return redirect("/membership")
    cur=db().execute("INSERT INTO memberships(user_id,plan_id,discount_id,original_price_cents,paid_price_cents,created) VALUES(?,?,?,?,?,?)", (g.user["id"], plan_id, discount["id"] if discount else None, plan["price_cents"], final, datetime.now().isoformat(timespec="seconds")))
    db().commit(); mid=cur.lastrowid
    try:
        stripe.api_key=STRIPE_KEY
        ss=stripe.checkout.Session.create(mode="payment", customer_email=g.user["email"], line_items=[{"quantity":1,"price_data":{"currency":os.environ.get("CURRENCY","usd"),"unit_amount":final,"product_data":{"name":plan["name"]}}}], metadata={"membership_id":str(mid)}, success_url=f"{BASE_URL}/membership?paid=1", cancel_url=f"{BASE_URL}/membership?cancelled=1")
        db().execute("UPDATE memberships SET stripe_session_id=? WHERE id=?", (ss.id, mid)); db().commit()
        return redirect(ss.url)
    except Exception as e:
        db().execute("DELETE FROM memberships WHERE id=?", (mid,)); db().commit(); print("Stripe membership error:",e); flash("Payment could not be started. No purchase was recorded."); return redirect("/membership")

@app.get("/account")
@login_required
def account():
    db().execute("UPDATE memberships SET status='expired' WHERE user_id=? AND status='active' AND expiry_date<?", (g.user["id"], date.today().isoformat())); db().commit()
    rows = db().execute("""SELECT b.*, s.name AS sname, s.price_cents FROM bookings b LEFT JOIN services s ON s.id=b.service
                           WHERE b.user_id=? ORDER BY b.day DESC, b.slot DESC""", (g.user["id"],)).fetchall()
    today = date.today().isoformat()
    up = [r for r in rows if r["day"] >= today and r["status"] in ("pending", "confirmed")]
    completed = [r for r in rows if r["status"] == "completed"]
    cancelled = [r for r in rows if r["status"] == "cancelled"]
    dashboard = dict(total_booked=len(rows), attended=len(completed), upcoming=len(up), completed=len(completed),
                     cancelled=len(cancelled), total_spend=sum(r["price_cents"] or 0 for r in rows if r["status"] in ("confirmed", "completed")))
    current_membership = db().execute("SELECT m.*, p.name AS plan_name, p.duration_label FROM memberships m JOIN membership_plans p ON p.id=m.plan_id WHERE m.user_id=? AND m.status='active' AND m.expiry_date>=? ORDER BY m.expiry_date DESC LIMIT 1", (g.user["id"], date.today().isoformat())).fetchone()
    membership_history = db().execute("SELECT m.*, p.name AS plan_name FROM memberships m JOIN membership_plans p ON p.id=m.plan_id WHERE m.user_id=? ORDER BY m.id DESC", (g.user["id"],)).fetchall()
    return render_template("account.html", upcoming=up, history=[r for r in rows if r not in up], dashboard=dashboard, current_membership=current_membership, membership_history=membership_history)

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
    obj = ev["data"]["object"]; metadata = obj.get("metadata") or {}; bid = metadata.get("booking_id"); mid = metadata.get("membership_id")
    if mid and ev["type"] == "checkout.session.completed":
        m = db().execute("SELECT m.*, p.duration_days FROM memberships m JOIN membership_plans p ON p.id=m.plan_id WHERE m.id=?", (mid,)).fetchone()
        if m:
            start=date.today(); expiry=start+timedelta(days=int(m["duration_days"]))
            db().execute("UPDATE memberships SET status='active',payment_status='paid',start_date=?,expiry_date=? WHERE id=?", (start.isoformat(), expiry.isoformat(), mid)); db().commit()
    elif mid and ev["type"] == "checkout.session.expired":
        db().execute("UPDATE memberships SET status='expired',payment_status='expired' WHERE id=? AND status='pending'", (mid,)); db().commit()
    elif bid and ev["type"] == "checkout.session.completed":
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
    elif view == "messages": ctx = dict(rows=q("SELECT m.*, t.name AS trainer_name FROM messages m LEFT JOIN trainers t ON t.id=m.trainer_id ORDER BY m.id DESC").fetchall())
    elif view == "content": ctx = dict(content=public_content(), achievements=q("SELECT * FROM achievements ORDER BY sort_order, id").fetchall())
    elif view == "trainers": ctx = dict(trainers=q("SELECT * FROM trainers ORDER BY name").fetchall())
    elif view == "memberships": ctx = dict(plans=q("SELECT * FROM membership_plans ORDER BY active DESC, price_cents").fetchall(), discounts=q("SELECT d.*, p.name AS plan_name FROM membership_discounts d JOIN membership_plans p ON p.id=d.plan_id ORDER BY d.id DESC").fetchall(), records=q("SELECT m.*, u.name AS user_name, u.email, p.name AS plan_name FROM memberships m JOIN users u ON u.id=m.user_id JOIN membership_plans p ON p.id=m.plan_id ORDER BY m.id DESC").fetchall())
    else: abort(404)
    return render_template("admin.html", view=view, **ctx)
@app.post("/admin/membership-plan")
@admin_required
def a_membership_plan():
    action=request.form.get("action","save"); pid=request.form.get("id")
    if action == "delete":
        if db().execute("SELECT 1 FROM memberships WHERE plan_id=?", (pid,)).fetchone(): db().execute("UPDATE membership_plans SET active=0 WHERE id=?", (pid,)); flash("Plan has purchase history, so it was disabled instead of deleted.")
        else: db().execute("DELETE FROM membership_plans WHERE id=?", (pid,)); flash("Membership plan removed.")
    else:
        name=request.form.get("name","").strip()[:100]
        try: days=int(request.form.get("duration_days",30)); price=round(float(request.form.get("price",0))*100)
        except ValueError: days=0; price=-1
        if not name or days<=0 or price<0: flash("Enter a valid plan name, duration, and non-negative price.")
        else:
            vals=(name,days,request.form.get("duration_label","").strip()[:80],request.form.get("description","").strip()[:1000],request.form.get("benefits","").strip()[:3000],request.form.get("included_sessions","").strip()[:120],price,int("active" in request.form))
            if pid: db().execute("UPDATE membership_plans SET name=?,duration_days=?,duration_label=?,description=?,benefits=?,included_sessions=?,price_cents=?,active=? WHERE id=?", vals+(pid,))
            else: db().execute("INSERT INTO membership_plans(name,duration_days,duration_label,description,benefits,included_sessions,price_cents,active,created) VALUES(?,?,?,?,?,?,?,?,?)", vals+(datetime.now().isoformat(timespec="seconds"),))
            flash("Membership plan saved.")
    db().commit(); return redirect("/admin/memberships")

@app.post("/admin/discount")
@admin_required
def a_discount():
    action=request.form.get("action","save"); did=request.form.get("id")
    if action == "delete": db().execute("DELETE FROM membership_discounts WHERE id=?", (did,)); flash("Discount removed.")
    else:
        try: plan_id=int(request.form.get("plan_id")); value=float(request.form.get("value",0))
        except ValueError: plan_id=0; value=-1
        plan=db().execute("SELECT price_cents FROM membership_plans WHERE id=?", (plan_id,)).fetchone(); kind=request.form.get("kind","percent")
        starts=request.form.get("starts","").strip(); expires=request.form.get("expires","") .strip()
        try:
            if starts: datetime.strptime(starts, "%Y-%m-%d")
            if expires: datetime.strptime(expires, "%Y-%m-%d")
            dates_valid=not (starts and expires) or starts<=expires
        except ValueError: dates_valid=False
        valid=plan and kind in ("percent","fixed") and dates_valid and value>=0 and ((kind=="percent" and value<=100) or (kind=="fixed" and round(value*100)<=plan["price_cents"]))
        if not valid: flash("Invalid discount. Check dates, percentages, and fixed prices.")
        else:
            cents_value= value if kind=="percent" else round(value*100)
            vals=(plan_id,kind,cents_value,starts,expires,int("active" in request.form))
            if did: db().execute("UPDATE membership_discounts SET plan_id=?,kind=?,value=?,starts=?,expires=?,active=? WHERE id=?", vals+(did,))
            else: db().execute("INSERT INTO membership_discounts(plan_id,kind,value,starts,expires,active,created) VALUES(?,?,?,?,?,?,?)", vals+(datetime.now().isoformat(timespec="seconds"),))
            flash("Discount saved.")
    db().commit(); return redirect("/admin/memberships")

@app.post("/admin/trainer")
@admin_required
def a_trainer():
    action = request.form.get("action", "save")
    tid = request.form.get("id")
    if action == "delete":
        db().execute("DELETE FROM trainers WHERE id=?", (tid,)); flash("Trainer removed.")
    else:
        name = request.form.get("name", "").strip()[:100]
        if not name: flash("Trainer name is required.")
        else:
            image = save_uploaded_image(request.files.get("image")) or request.form.get("existing_image", "").strip()[:500]
            values = (name, image, request.form.get("bio", "").strip()[:3000], request.form.get("specialization", "").strip()[:300], request.form.get("certifications", "").strip()[:500], request.form.get("experience", "").strip()[:160], request.form.get("programs", "").strip()[:500], request.form.get("availability", "").strip()[:300])
            if tid: db().execute("UPDATE trainers SET name=?,image=?,bio=?,specialization=?,certifications=?,experience=?,programs=?,availability=? WHERE id=?", values + (tid,))
            else: db().execute("INSERT INTO trainers(name,image,bio,specialization,certifications,experience,programs,availability,created) VALUES(?,?,?,?,?,?,?,?,?)", values + (datetime.now().isoformat(timespec="seconds"),))
            flash("Trainer saved.")
    db().commit(); return redirect("/admin/trainers")
@app.post("/admin/content")
@admin_required
def a_content():
    action = request.form.get("action", "save")
    if action == "delete_achievement":
        db().execute("DELETE FROM achievements WHERE id=?", (request.form.get("id"),))
    elif action == "save_achievement":
        title = request.form.get("title", "").strip()
        if title:
            values = (title, request.form.get("description", "").strip(), request.form.get("year", "").strip(), int(request.form.get("sort_order") or 0))
            if request.form.get("id"):
                db().execute("UPDATE achievements SET title=?,description=?,year=?,sort_order=? WHERE id=?", values + (request.form["id"],))
            else:
                db().execute("INSERT INTO achievements(title,description,year,sort_order) VALUES(?,?,?,?)", values)
        else: flash("Achievement needs a title.")
    else:
        keys = ("gym_name", "gym_description", "gym_facilities", "gym_address", "gym_map_url", "gym_hours", "gym_phone", "gym_email", "gym_social", "owner_name", "owner_image", "owner_bio", "client_retention", "client_experience")
        db().executemany("INSERT INTO site_content(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", [(key, request.form.get(key, "").strip()[:4000]) for key in keys])
        flash("Public gym information saved.")
    db().commit()
    return redirect("/admin/content")

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
