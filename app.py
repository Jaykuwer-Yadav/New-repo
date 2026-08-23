# pyright: reportMissingImports=false
# pylint: disable=import-error
import os
import sys
import glob
import json
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

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "birthday_memories_secret_key_for_sessions_2026")
app.config["UPLOAD_FOLDER"] = os.path.join("static", "uploads")
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64MB max upload

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs("templates", exist_ok=True)
os.makedirs(os.path.join("static", "css"), exist_ok=True)
os.makedirs(os.path.join("static", "js"), exist_ok=True)

# ----------------------------------------------------
# 1. FIREBASE ADMIN & CLOUD FIRESTORE INITIALIZATION
# ----------------------------------------------------
firebase_app = None
firebase_db = None
firebase_bucket = None

try:
    import firebase_admin
    from firebase_admin import credentials, firestore, storage
    
    cred = None
    env_creds = os.environ.get("FIREBASE_CREDENTIALS_JSON")
    if env_creds:
        try:
            creds_dict = json.loads(env_creds)
            cred = credentials.Certificate(creds_dict)
        except Exception as e:
            print(f"[Firebase] Error parsing FIREBASE_CREDENTIALS_JSON: {e}")

    if not cred:
        key_files = glob.glob("*firebase-adminsdk*.json") + glob.glob("firebase-key.json") + glob.glob("serviceAccountKey.json")
        if key_files and os.path.exists(key_files[0]):
            cred = credentials.Certificate(key_files[0])

    if cred:
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

# ----------------------------------------------------
# 2. LOCAL SQLITE BACKUP INITIALIZATION
# ----------------------------------------------------
DB_PATH = "birthday.db"

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_local_db():
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS achievements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            badge_name TEXT NOT NULL,
            earned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, badge_name)
        )
    """)
    
    # Default Admin & Jay user accounts
    admin_exists = cursor.execute("SELECT id FROM users WHERE email = 'admin@bday.com'").fetchone()
    if not admin_exists:
        cursor.execute(
            "INSERT INTO users (email, password_hash, plain_password, role, display_name, birthday_date) VALUES (?, ?, ?, ?, ?, ?)",
            ("admin@bday.com", generate_password_hash("admin123"), "admin123", "admin", "Admin", None)
        )
    else:
        cursor.execute("UPDATE users SET plain_password = COALESCE(plain_password, 'admin123') WHERE email = 'admin@bday.com'")
        
    jay_exists = cursor.execute("SELECT id FROM users WHERE email = 'jay@bday.com'").fetchone()
    if not jay_exists:
        default_bday = (datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M")
        cursor.execute(
            "INSERT INTO users (email, password_hash, plain_password, role, display_name, birthday_date, plain_lock_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("jay@bday.com", generate_password_hash("jay123"), "jay123", "user", "Jay", default_bday, "secret123")
        )
    else:
        cursor.execute("UPDATE users SET plain_password = COALESCE(plain_password, 'jay123') WHERE email = 'jay@bday.com'")
        
    conn.commit()
    conn.close()

init_local_db()

# ----------------------------------------------------
# 3. UNIFIED DATA ACCESS LAYER (FIRESTORE + SQLITE SYNC)
# ----------------------------------------------------

def get_user_by_email(email):
    email = email.strip().lower()
    # 1. Try Firestore
    if firebase_db:
        try:
            docs = firebase_db.collection("users").where("email", "==", email).limit(1).get()
            for doc in docs:
                data = doc.to_dict()
                data["id"] = data.get("id") or doc.id
                return data
        except Exception as e:
            print(f"[Firestore] get_user_by_email fallback: {e}")
            
    # 2. Fallback to SQLite
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()
    return dict(user) if user else None

def get_user_by_id(user_id):
    # 1. Try Firestore
    if firebase_db:
        try:
            doc = firebase_db.collection("users").document(str(user_id)).get()
            if doc.exists:
                data = doc.to_dict()
                data["id"] = data.get("id") or doc.id
                return data
            # Also search by numeric id field
            docs = firebase_db.collection("users").where("id", "==", int(user_id)).limit(1).get()
            for d in docs:
                data = d.to_dict()
                data["id"] = data.get("id") or d.id
                return data
        except Exception as e:
            print(f"[Firestore] get_user_by_id fallback: {e}")
            
    # 2. Fallback to SQLite
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(user) if user else None

def get_all_standard_users():
    # 1. Try Firestore
    if firebase_db:
        try:
            docs = firebase_db.collection("users").where("role", "==", "user").get()
            users_list = []
            for doc in docs:
                data = doc.to_dict()
                data["id"] = data.get("id") or doc.id
                users_list.append(data)
            if users_list:
                return sorted(users_list, key=lambda x: str(x.get("id", "")))
        except Exception as e:
            print(f"[Firestore] get_all_standard_users fallback: {e}")
            
    # 2. Fallback to SQLite
    conn = get_db_connection()
    users = conn.execute("SELECT * FROM users WHERE role = 'user' ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(u) for u in users]

def save_new_user(display_name, email, password, birthday_date, lock_key="", profile_pic=None, scratch_reward=""):
    email = email.strip().lower()
    pass_hash = generate_password_hash(password)
    lock_hash = generate_password_hash(lock_key) if lock_key else None
    
    # Save to SQLite
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO users 
           (email, password_hash, plain_password, role, display_name, birthday_date, lock_key_hash, plain_lock_key, profile_pic, scratch_reward) 
           VALUES (?, ?, ?, 'user', ?, ?, ?, ?, ?, ?)""",
        (email, pass_hash, password, display_name, birthday_date, lock_hash, lock_key if lock_key else None, profile_pic, scratch_reward)
    )
    user_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    # Save to Firestore permanently
    if firebase_db:
        try:
            firebase_db.collection("users").document(str(user_id)).set({
                "id": user_id,
                "email": email,
                "password_hash": pass_hash,
                "plain_password": password,
                "role": "user",
                "display_name": display_name,
                "birthday_date": birthday_date,
                "lock_key_hash": lock_hash,
                "plain_lock_key": lock_key if lock_key else None,
                "profile_pic": profile_pic,
                "scratch_reward": scratch_reward,
                "created_at": firestore.SERVER_TIMESTAMP
            })
            print(f"[Firestore] User '{display_name}' saved permanently to cloud database!")
        except Exception as e:
            print(f"[Firestore] save_new_user error: {e}")
            
    return user_id

def delete_user_by_id(user_id):
    # SQLite
    conn = get_db_connection()
    conn.execute("DELETE FROM achievements WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM memories WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM notes WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    
    # Firestore
    if firebase_db:
        try:
            firebase_db.collection("users").document(str(user_id)).delete()
        except Exception as e:
            print(f"[Firestore] delete_user error: {e}")

def update_user_profile_pic(user_id, photo_path):
    conn = get_db_connection()
    conn.execute("UPDATE users SET profile_pic = ? WHERE id = ?", (photo_path, user_id))
    conn.commit()
    conn.close()
    
    if firebase_db:
        try:
            firebase_db.collection("users").document(str(user_id)).set({
                "profile_pic": photo_path,
                "updated_at": firestore.SERVER_TIMESTAMP
            }, merge=True)
            print(f"[Firestore] User {user_id} profile_pic updated in cloud!")
        except Exception as e:
            print(f"[Firestore] update_user_profile_pic error: {e}")

# ----------------------------------------------------
# 4. HELPER FUNCTIONS & MIDDLEWARES
# ----------------------------------------------------

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
    users = get_all_standard_users()
    now = datetime.now()
    upcoming = []
    
    for u in users:
        if not u.get("birthday_date"):
            continue
        try:
            bdate = datetime.strptime(u["birthday_date"], "%Y-%m-%dT%H:%M")
            diff = bdate - now
            upcoming.append({
                "id": u["id"],
                "display_name": u.get("display_name") or u.get("email"),
                "email": u.get("email"),
                "birthday_date": u.get("birthday_date"),
                "profile_pic": u.get("profile_pic"),
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

# ----------------------------------------------------
# 5. ROUTES
# ----------------------------------------------------

@app.route("/")
def index():
    if not is_logged_in():
        return redirect(url_for("login"))
    
    current_user = get_user_by_id(session["user_id"])
    if current_user:
        session["birthday_date"] = current_user.get("birthday_date")
        session["display_name"] = current_user.get("display_name")
        
    conn = get_db_connection()
    badges = conn.execute("SELECT badge_name, earned_at FROM achievements WHERE user_id = ?", (session["user_id"],)).fetchall()
    note_count = conn.execute("SELECT COUNT(*) as cnt FROM notes WHERE user_id = ?", (session["user_id"],)).fetchone()["cnt"]
    recent_memories = conn.execute("SELECT * FROM memories WHERE user_id = ? ORDER BY created_at DESC LIMIT 3", (session["user_id"],)).fetchall()
    conn.close()
    
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
            
        user = get_user_by_email(email)
        
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["user_email"] = user["email"]
            session["user_role"] = user.get("role", "user")
            session["display_name"] = user.get("display_name") or user["email"].split("@")[0]
            session["birthday_date"] = user.get("birthday_date")
            flash(f"Welcome back, {session['display_name']}! 🎉", "success")
            return redirect(url_for("index"))
        else:
            flash("Invalid email or password.", "error")
            
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))

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
        users_list = get_all_standard_users()
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
        users_list = get_all_standard_users()
        conn.close()
        return render_template("memories.html", memories=memories_list, users=users_list)
    else:
        memories_list = conn.execute(
            "SELECT * FROM memories WHERE user_id = ? ORDER BY created_at DESC", 
            (session["user_id"],)
        ).fetchall()
        conn.close()
        return render_template("memories.html", memories=memories_list)

# ----------------------------------------------------
# 6. ACTION & API ENDPOINTS
# ----------------------------------------------------

@app.route("/api/upload-hero-photo", methods=["POST"])
def upload_hero_photo():
    if not is_logged_in():
        return redirect(url_for("login"))
        
    target_user_id = session["user_id"]
    redirect_dest = request.form.get("redirect_to", "index")
    
    if session.get("user_role") == "admin" and request.form.get("user_id"):
        target_user_id = request.form.get("user_id")
        
    if "hero_image" not in request.files:
        flash("No image file selected.", "error")
        return redirect(url_for(redirect_dest))
        
    file = request.files["hero_image"]
    if not file or file.filename == "":
        flash("No image file selected.", "error")
        return redirect(url_for(redirect_dest))
        
    safe_name = secure_filename(file.filename)
    unique_name = f"hero_{target_user_id}_{int(datetime.now().timestamp())}_{safe_name}"
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], unique_name)
    file.save(filepath)
    
    photo_path = f"/static/uploads/{unique_name}"
    
    if firebase_bucket:
        try:
            blob = firebase_bucket.blob(f"hero_photos/{unique_name}")
            blob.upload_from_filename(filepath)
            blob.make_public()
            if blob.public_url:
                photo_path = blob.public_url
        except Exception as e:
            print(f"[Firebase Storage] Hero upload notice: {e}")
    
    update_user_profile_pic(target_user_id, photo_path)
    
    flash("Hero portrait photo updated successfully! 📸", "success")
    return redirect(url_for(redirect_dest))

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
            print(f"[Firestore] note sync notice: {e}")
    
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
    
    if firebase_bucket:
        try:
            blob = firebase_bucket.blob(f"memories/{unique_filename}")
            blob.upload_from_filename(filepath)
            blob.make_public()
            if blob.public_url:
                media_web_path = blob.public_url
        except Exception as e:
            print(f"[Firebase Storage] Memory upload notice: {e}")
    
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
        user_data = get_user_by_id(memory["user_id"])
        if user_data and user_data.get("lock_key_hash") and check_password_hash(user_data["lock_key_hash"], password):
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

# ----------------------------------------------------
# 7. ADMIN PANEL MANAGEMENT
# ----------------------------------------------------

@app.route("/admin")
@admin_required
def admin_panel():
    users_list = get_all_standard_users()
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
        
    existing = get_user_by_email(email)
    if existing:
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
            
    try:
        save_new_user(
            display_name=display_name,
            email=email,
            password=password,
            birthday_date=birthday_date,
            lock_key=user_lock_key,
            profile_pic=profile_pic,
            scratch_reward=scratch_reward
        )
        flash(f"Account for '{display_name}' created successfully! Saved permanently.", "success")
    except Exception as e:
        flash(f"Error creating user: {str(e)}", "error")
        
    return redirect(url_for("admin_panel"))

@app.route("/admin/delete-user/<int:user_id>", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    try:
        delete_user_by_id(user_id)
        flash("User account, memories, and notes revoked.", "success")
    except Exception as e:
        flash(f"Error deleting user: {str(e)}", "error")
        
    return redirect(url_for("admin_panel"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
