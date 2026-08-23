# pyright: reportMissingImports=false
# pylint: disable=import-error
import os
import sys
import glob
import sqlite3
from datetime import datetime, timedelta
from functools import wraps

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
    from werkzeug.security import generate_password_hash, check_password_hash
    from werkzeug.utils import secure_filename
except ImportError:
    pass

# Firebase Admin SDK Initialization
firebase_app = None
firebase_db = None
firebase_bucket = None

try:
    import firebase_admin
    from firebase_admin import credentials, firestore, storage
    
    # Locate any firebase admin sdk private key in directory
    key_files = glob.glob("*firebase-adminsdk*.json") + glob.glob("firebase-key.json") + glob.glob("serviceAccountKey.json")
    if key_files and os.path.exists(key_files[0]):
        cred = credentials.Certificate(key_files[0])
        # Default bucket name for Firebase project
        bucket_name = os.environ.get("FIREBASE_STORAGE_BUCKET", f"{cred.project_id}.appspot.com")
        firebase_app = firebase_admin.initialize_app(cred, {
            "storageBucket": bucket_name
        })
        firebase_db = firestore.client()
        try:
            firebase_bucket = storage.bucket()
        except Exception:
            firebase_bucket = None
        print(f"[Firebase] Initialized successfully for project: {cred.project_id}")
except Exception as e:
    print(f"[Storage] Running with local SQLite backend (Firebase optional: {e})")

app = Flask(__name__)
app.secret_key = "birthday_memories_secret_key_for_sessions_2026"
app.config["UPLOAD_FOLDER"] = os.path.join("static", "uploads")
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64MB max upload

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs("templates", exist_ok=True)
os.makedirs(os.path.join("static", "css"), exist_ok=True)
os.makedirs(os.path.join("static", "js"), exist_ok=True)

DB_PATH = "birthday.db"

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            plain_password TEXT,
            role TEXT DEFAULT 'user',
            display_name TEXT,
            birthday_date TEXT,
            lock_key_hash TEXT,
            plain_lock_key TEXT,
            profile_pic TEXT,
            scratch_reward TEXT DEFAULT '🎉 Congratulations! You unlocked a special birthday surprise and gift from all of us! 🎁'
        )
    """)
    
    for col_def in [
        ("role", "TEXT DEFAULT 'user'"),
        ("display_name", "TEXT"),
        ("birthday_date", "TEXT"),
        ("lock_key_hash", "TEXT"),
        ("plain_password", "TEXT"),
        ("plain_lock_key", "TEXT"),
        ("profile_pic", "TEXT"),
        ("scratch_reward", "TEXT")
    ]:
        try:
            cursor.execute(f"ALTER TABLE users ADD COLUMN {col_def[0]} {col_def[1]}")
        except sqlite3.OperationalError:
            pass
        
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            media_path TEXT NOT NULL,
            is_video INTEGER DEFAULT 0,
            is_locked INTEGER DEFAULT 0,
            lock_password_hash TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            sender_name TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            icon TEXT DEFAULT '💌',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS achievements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            badge_name TEXT NOT NULL,
            badge_icon TEXT,
            earned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            UNIQUE(user_id, badge_name)
        )
    """)
    
    admin_email = "admin@bday.com"
    user_email = "jay@bday.com"
    
    admin_exists = cursor.execute("SELECT id FROM users WHERE email = ?", (admin_email,)).fetchone()
    if not admin_exists:
        cursor.execute(
            "INSERT INTO users (email, password_hash, plain_password, role, display_name, birthday_date) VALUES (?, ?, ?, ?, ?, ?)",
            (admin_email, generate_password_hash("admin123"), "admin123", "admin", "Admin", None)
        )
    else:
        cursor.execute("UPDATE users SET role = 'admin', plain_password = COALESCE(plain_password, 'admin123') WHERE email = ?", (admin_email,))
        
    user_exists = cursor.execute("SELECT id FROM users WHERE email = ?", (user_email,)).fetchone()
    if not user_exists:
        default_bday = (datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M")
        cursor.execute(
            "INSERT INTO users (email, password_hash, plain_password, role, display_name, birthday_date, plain_lock_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_email, generate_password_hash("jay123"), "jay123", "user", "Jay", default_bday, "secret123")
        )
    else:
        cursor.execute("UPDATE users SET plain_password = COALESCE(plain_password, 'jay123') WHERE email = ?", (user_email,))
        
    conn.commit()
    conn.close()

init_db()

# --- HELPER FUNCTIONS ---
def is_logged_in():
    return "user_id" in session

def is_birthday_arrived():
    if session.get("user_role") == "admin":
        return True
    bday_str = session.get("birthday_date")
    if not bday_str:
        return True
    try:
        bday_time = datetime.strptime(bday_str, "%Y-%m-%dT%H:%M")
        return datetime.now() >= bday_time
    except Exception:
        return True

def birthday_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_logged_in():
            return redirect(url_for("login"))
        # Standard users are locked out of all tabs apart from timer until their birthday arrives
        if not is_birthday_arrived():
            flash("🔒 This section is locked! It will open when your birthday countdown reaches zero. ⏳", "info")
            return redirect(url_for("timer"))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_logged_in():
            return redirect(url_for("login"))
        if session.get("user_role") != "admin":
            flash("Unauthorized: Admin access required.", "error")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated_function

def get_nearest_birthdays():
    conn = get_db_connection()
    users = conn.execute("SELECT id, display_name, email, birthday_date, profile_pic FROM users WHERE role = 'user' AND birthday_date IS NOT NULL").fetchall()
    conn.close()
    
    now = datetime.now()
    upcoming = []
    
    for u in users:
        try:
            bdate = datetime.strptime(u["birthday_date"], "%Y-%m-%dT%H:%M")
            diff = bdate - now
            upcoming.append({
                "id": u["id"],
                "display_name": u["display_name"],
                "email": u["email"],
                "birthday_date": u["birthday_date"],
                "profile_pic": u["profile_pic"],
                "diff_seconds": diff.total_seconds(),
                "is_past": diff.total_seconds() <= 0,
                "days_left": diff.days,
                "hours_left": int((diff.total_seconds() % 86400) // 3600)
            })
        except Exception:
            continue
            
    future_bday = sorted([u for u in upcoming if not u["is_past"]], key=lambda x: x["diff_seconds"])
    past_bday = sorted([u for u in upcoming if u["is_past"]], key=lambda x: abs(x["diff_seconds"]))
    
    sorted_upcoming = future_bday + past_bday
    nearest = sorted_upcoming[0] if sorted_upcoming else None
    return nearest, sorted_upcoming

@app.context_processor
def inject_user():
    return dict(
        is_logged_in=is_logged_in(), 
        user_email=session.get("user_email"),
        user_role=session.get("user_role"),
        display_name=session.get("display_name"),
        birthday_date=session.get("birthday_date"),
        is_birthday_arrived=is_birthday_arrived()
    )

# --- ROUTES ---

@app.route("/")
def index():
    if not is_logged_in():
        return redirect(url_for("login"))
    
    conn = get_db_connection()
    badges = conn.execute("SELECT badge_name, earned_at FROM achievements WHERE user_id = ?", (session["user_id"],)).fetchall()
    current_user = conn.execute("SELECT id, birthday_date, display_name, profile_pic, scratch_reward FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    note_count = conn.execute("SELECT COUNT(*) as cnt FROM notes WHERE user_id = ?", (session["user_id"],)).fetchone()["cnt"]
    recent_memories = conn.execute("SELECT * FROM memories WHERE user_id = ? ORDER BY created_at DESC LIMIT 3", (session["user_id"],)).fetchall()
    conn.close()
    
    if current_user:
        session["birthday_date"] = current_user["birthday_date"]
        session["display_name"] = current_user["display_name"]
        
    nearest_bday, all_upcoming = None, []
    if session.get("user_role") == "admin":
        nearest_bday, all_upcoming = get_nearest_birthdays()
        
    earned_badges = [b["badge_name"] for b in badges]
    
    return render_template(
        "dashboard.html", 
        earned_badges=earned_badges, 
        birthday_date=session.get("birthday_date"),
        note_count=note_count,
        user_data=current_user,
        recent_memories=recent_memories,
        nearest_bday=nearest_bday,
        all_upcoming=all_upcoming
    )

@app.route("/login", methods=["GET", "POST"])
def login():
    if is_logged_in():
        return redirect(url_for("index"))
        
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        
        if not email or not password:
            flash("Please enter both email and password.", "error")
            return render_template("login.html")
            
        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()
        
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["user_email"] = user["email"]
            session["user_role"] = user["role"]
            session["display_name"] = user["display_name"] or user["email"].split("@")[0]
            session["birthday_date"] = user["birthday_date"]
            flash(f"Welcome back, {session['display_name']}! 🎉", "success")
            return redirect(url_for("index"))
        else:
            flash("Invalid email or password.", "error")
            
    return render_template("login.html")

@app.route("/signup", methods=["POST"])
def signup():
    flash("Public registration is disabled. Credentials must be obtained from the Admin.", "error")
    return redirect(url_for("login"))

@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))

# --- FEATURE PAGES WITH STRICT BIRTHDAY ACCESS CONTROL ---

@app.route("/timer")
def timer():
    if not is_logged_in():
        return redirect(url_for("login"))
    return render_template("timer.html")

@app.route("/cake")
@birthday_required
def cake():
    return render_template("cake.html")

@app.route("/notes")
@birthday_required
def notes():
    conn = get_db_connection()
    if session.get("user_role") == "admin":
        notes_list = conn.execute("""
            SELECT n.*, u.display_name as recipient_name, u.email as recipient_email 
            FROM notes n 
            LEFT JOIN users u ON n.user_id = u.id 
            ORDER BY n.created_at DESC
        """).fetchall()
        users_list = conn.execute("SELECT id, display_name, email FROM users WHERE role = 'user' ORDER BY display_name ASC").fetchall()
        conn.close()
        return render_template("notes.html", notes=notes_list, users=users_list)
    else:
        notes_list = conn.execute(
            "SELECT * FROM notes WHERE user_id = ? ORDER BY created_at DESC", 
            (session["user_id"],)
        ).fetchall()
        conn.close()
        return render_template("notes.html", notes=notes_list)

@app.route("/memories")
@birthday_required
def memories():
    conn = get_db_connection()
    if session.get("user_role") == "admin":
        memories_list = conn.execute("""
            SELECT m.*, u.display_name as recipient_name, u.email as recipient_email 
            FROM memories m 
            LEFT JOIN users u ON m.user_id = u.id 
            ORDER BY m.created_at DESC
        """).fetchall()
        users_list = conn.execute("SELECT id, display_name, email FROM users WHERE role = 'user' ORDER BY display_name ASC").fetchall()
        conn.close()
        return render_template("memories.html", memories=memories_list, users=users_list)
    else:
        memories_list = conn.execute(
            "SELECT * FROM memories WHERE user_id = ? ORDER BY created_at DESC", 
            (session["user_id"],)
        ).fetchall()
        conn.close()
        return render_template("memories.html", memories=memories_list)

# --- API ENDPOINTS ---

@app.route("/api/upload-hero-photo", methods=["POST"])
def upload_hero_photo():
    if not is_logged_in():
        return redirect(url_for("login"))
        
    if "hero_image" not in request.files:
        flash("No image file selected.", "error")
        return redirect(url_for("index"))
        
    file = request.files["hero_image"]
    if not file or file.filename == "":
        flash("No image file selected.", "error")
        return redirect(url_for("index"))
        
    safe_name = secure_filename(file.filename)
    unique_name = f"hero_{session['user_id']}_{int(datetime.now().timestamp())}_{safe_name}"
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], unique_name)
    file.save(filepath)
    
    photo_path = f"/static/uploads/{unique_name}"
    
    # Upload to Firebase Storage if connected
    if firebase_bucket:
        try:
            blob = firebase_bucket.blob(f"hero_photos/{unique_name}")
            blob.upload_from_filename(filepath)
            blob.make_public()
            if blob.public_url:
                photo_path = blob.public_url
        except Exception as e:
            print(f"Firebase Storage upload notice: {e}")
    
    conn = get_db_connection()
    conn.execute("UPDATE users SET profile_pic = ? WHERE id = ?", (photo_path, session["user_id"]))
    conn.commit()
    conn.close()
    
    flash("Hero portrait photo updated! 📸", "success")
    return redirect(url_for("index"))

@app.route("/api/add-note", methods=["POST"])
@admin_required
def add_note():
    sender_name = request.form.get("sender_name", "").strip() or session.get("display_name", "A Friend")
    title = request.form.get("title", "").strip()
    message = request.form.get("message", "").strip()
    icon = request.form.get("icon", "💌")
    target_user_id = request.form.get("user_id")
        
    if not title or not message or not target_user_id:
        flash("Title, recipient, and message are required for the note.", "error")
        return redirect(url_for("notes"))
        
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO notes (user_id, sender_name, title, message, icon) VALUES (?, ?, ?, ?, ?)",
        (target_user_id, sender_name, title, message, icon)
    )
    conn.commit()
    conn.close()
    
    # Sync with Firestore if active
    if firebase_db:
        try:
            firebase_db.collection("notes").add({
                "user_id": target_user_id,
                "sender_name": sender_name,
                "title": title,
                "message": message,
                "icon": icon,
                "created_at": firestore.SERVER_TIMESTAMP
            })
        except Exception as e:
            print(f"Firestore note sync notice: {e}")
    
    flash("Birthday Note & Letter added successfully! 💌", "success")
    return redirect(url_for("notes"))

@app.route("/api/delete-note/<int:note_id>", methods=["POST"])
@admin_required
def delete_note(note_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    conn.commit()
    conn.close()
    flash("Note removed successfully.", "info")
    return redirect(url_for("notes"))

@app.route("/api/earn-badge", methods=["POST"])
def earn_badge():
    if not is_logged_in():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
        
    data = request.get_json(silent=True) or {}
    badge_name = data.get("badge_name")
    
    if not badge_name:
        return jsonify({"status": "error", "message": "Badge name required"}), 400
        
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO achievements (user_id, badge_name) VALUES (?, ?)",
            (session["user_id"], badge_name)
        )
        conn.commit()
        return jsonify({"status": "success", "message": f"Earned badge: {badge_name}"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()

@app.route("/api/upload-memory", methods=["POST"])
def upload_memory():
    if not is_logged_in():
        return redirect(url_for("login"))
    if session.get("user_role") != "admin":
        flash("Unauthorized: Only administrators can upload memories.", "error")
        return redirect(url_for("memories"))
        
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    media_type = request.form.get("media_type", "auto")
    is_locked = 1 if request.form.get("is_locked") == "on" else 0
    lock_password = request.form.get("lock_password")
    target_user_id = request.form.get("user_id")
    
    if "media" not in request.files or not title or not target_user_id:
        flash("Title, recipient selection, and a media file are required.", "error")
        return redirect(url_for("memories"))
    
    file = request.files["media"]
    if not file or file.filename == "":
        flash("No media file selected.", "error")
        return redirect(url_for("memories"))
    
    filename_lower = file.filename.lower()
    video_exts = (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".ogv")
    
    is_video = 0
    if media_type == "video":
        is_video = 1
    elif media_type == "image":
        is_video = 0
    elif (file.mimetype and file.mimetype.startswith("video/")) or filename_lower.endswith(video_exts):
        is_video = 1
    
    safe_name = secure_filename(file.filename)
    if not safe_name:
        safe_name = "upload_" + ("video.mp4" if is_video else "photo.jpg")
    unique_filename = f"{target_user_id}_{int(datetime.now().timestamp())}_{safe_name}"
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], unique_filename)
    file.save(filepath)
    
    media_web_path = f"/static/uploads/{unique_filename}"
    
    # Upload to Firebase Storage if available
    if firebase_bucket:
        try:
            blob = firebase_bucket.blob(f"memories/{unique_filename}")
            blob.upload_from_filename(filepath)
            blob.make_public()
            if blob.public_url:
                media_web_path = blob.public_url
        except Exception as e:
            print(f"Firebase Storage memory upload notice: {e}")
    
    lock_password_hash = None
    if is_locked and lock_password:
        lock_password_hash = generate_password_hash(lock_password)
    
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO memories (user_id, title, description, media_path, is_video, is_locked, lock_password_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (target_user_id, title, description, media_web_path, is_video, is_locked, lock_password_hash)
    )
    conn.execute(
        "INSERT OR IGNORE INTO achievements (user_id, badge_name) VALUES (?, 'Memory Keeper')",
        (target_user_id,)
    )
    conn.commit()
    conn.close()
    
    media_label = "Video" if is_video else "Photo"
    flash(f"{media_label} memory '{title}' added to recipient chest! 📸", "success")
    return redirect(url_for("memories"))

@app.route("/api/unlock-memory", methods=["POST"])
def unlock_memory():
    if not is_logged_in():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
        
    data = request.get_json(silent=True) or {}
    memory_id = data.get("memory_id")
    password = data.get("password", "")
    
    if not memory_id or not password:
        return jsonify({"status": "error", "message": "Missing credentials"}), 400
        
    conn = get_db_connection()
    if session.get("user_role") == "admin":
        memory = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    else:
        memory = conn.execute("SELECT * FROM memories WHERE id = ? AND user_id = ?", (memory_id, session["user_id"])).fetchone()
    conn.close()
    
    if not memory:
        return jsonify({"status": "error", "message": "Memory not found"}), 404
        
    unlocked = False
    if memory["lock_password_hash"] and check_password_hash(memory["lock_password_hash"], password):
        unlocked = True
    else:
        conn = get_db_connection()
        user_data = conn.execute("SELECT lock_key_hash FROM users WHERE id = ?", (memory["user_id"],)).fetchone()
        conn.close()
        if user_data and user_data["lock_key_hash"] and check_password_hash(user_data["lock_key_hash"], password):
            unlocked = True
            
    if unlocked:
        return jsonify({
            "status": "success",
            "media_path": memory["media_path"],
            "is_video": memory["is_video"],
            "description": memory["description"]
        })
    else:
        return jsonify({"status": "error", "message": "Incorrect password"}), 403

# --- ADMIN PANEL ROUTES ---

@app.route("/admin")
@admin_required
def admin_panel():
    conn = get_db_connection()
    users_list = conn.execute("SELECT id, email, role, display_name, birthday_date, plain_password, plain_lock_key, profile_pic, scratch_reward FROM users WHERE role = 'user' ORDER BY id DESC").fetchall()
    conn.close()
    nearest_bday, all_upcoming = get_nearest_birthdays()
    return render_template("admin.html", users=users_list, nearest_bday=nearest_bday, all_upcoming=all_upcoming)

@app.route("/admin/create-user", methods=["POST"])
@admin_required
def admin_create_user():
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    display_name = request.form.get("display_name", "").strip()
    birthday_date = request.form.get("birthday_date")
    user_lock_key = request.form.get("user_lock_key", "").strip()
    scratch_reward = request.form.get("scratch_reward", "").strip() or "🎉 Congratulations! You unlocked a special birthday surprise and gift from all of us! 🎁"
    
    if not email or not password or not display_name or not birthday_date:
        flash("Display Name, Email, Password, and Birthday Date are required.", "error")
        return redirect(url_for("admin_panel"))
        
    conn = get_db_connection()
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        conn.close()
        flash("A user with that email already exists.", "error")
        return redirect(url_for("admin_panel"))
        
    profile_pic = None
    if "profile_pic" in request.files:
        file = request.files["profile_pic"]
        if file and file.filename != "":
            safe_name = secure_filename(file.filename)
            unique_name = f"profile_{int(datetime.now().timestamp())}_{safe_name}"
            filepath = os.path.join(app.config["UPLOAD_FOLDER"], unique_name)
            file.save(filepath)
            profile_pic = f"/static/uploads/{unique_name}"
            
    password_hash = generate_password_hash(password)
    lock_key_hash = generate_password_hash(user_lock_key) if user_lock_key else None
    
    try:
        conn.execute(
            """INSERT INTO users 
               (email, password_hash, plain_password, role, display_name, birthday_date, lock_key_hash, plain_lock_key, profile_pic, scratch_reward) 
               VALUES (?, ?, ?, 'user', ?, ?, ?, ?, ?, ?)""",
            (email, password_hash, password, display_name, birthday_date, lock_key_hash, user_lock_key if user_lock_key else None, profile_pic, scratch_reward)
        )
        conn.commit()
        flash(f"Account for '{display_name}' created successfully! Credentials saved.", "success")
    except Exception as e:
        flash(f"Error creating user: {str(e)}", "error")
    finally:
        conn.close()
        
    return redirect(url_for("admin_panel"))

@app.route("/admin/delete-user/<int:user_id>", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM achievements WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM memories WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM notes WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        flash("User account, memories, and notes revoked.", "success")
    except Exception as e:
        flash(f"Error deleting user: {str(e)}", "error")
    finally:
        conn.close()
        
    return redirect(url_for("admin_panel"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

