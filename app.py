# pyright: reportMissingImports=false
# pylint: disable=import-error
import os
import sys
import glob
import json
import uuid
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

import firebase_admin
from firebase_admin import credentials, firestore, storage

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "birthday_memories_secret_key_for_sessions_2026")
app.config["UPLOAD_FOLDER"] = os.path.join("static", "uploads")
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64MB max upload

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs("templates", exist_ok=True)
os.makedirs(os.path.join("static", "css"), exist_ok=True)
os.makedirs(os.path.join("static", "js"), exist_ok=True)

# ----------------------------------------------------
# 1. PURE FIREBASE INITIALIZATION
# ----------------------------------------------------
firebase_app = None
firebase_db = None
firebase_bucket = None

cred = None

# 1. Check all possible Environment Variable names
for env_name in ["FIREBASE_CREDENTIALS_JSON", "FIREBASE_CREDENTIALS", "FIREBASE_KEY", "GOOGLE_APPLICATION_CREDENTIALS_JSON"]:
    env_val = os.environ.get(env_name)
    if env_val:
        try:
            cleaned = env_val.strip()
            if (cleaned.startswith("'{") and cleaned.endswith("}'")) or (cleaned.startswith('"{') and cleaned.endswith('}"')):
                cleaned = cleaned[1:-1]
            creds_dict = json.loads(cleaned)
            cred = credentials.Certificate(creds_dict)
            print(f"[Firebase] Loaded credentials from environment variable: {env_name}")
            break
        except Exception as e:
            print(f"[Firebase] Error parsing {env_name}: {e}")

# 2. Check for Base64 encoded environment variable
if not cred and os.environ.get("FIREBASE_CREDENTIALS_BASE64"):
    try:
        import base64
        decoded = base64.b64decode(os.environ.get("FIREBASE_CREDENTIALS_BASE64")).decode("utf-8")
        creds_dict = json.loads(decoded)
        cred = credentials.Certificate(creds_dict)
        print("[Firebase] Loaded credentials from FIREBASE_CREDENTIALS_BASE64.")
    except Exception as e:
        print(f"[Firebase] Error parsing FIREBASE_CREDENTIALS_BASE64: {e}")

# 3. Check for Secret Files on Render (/etc/secrets/...) or local directory
if not cred:
    search_paths = [
        "/etc/secrets/serviceAccountKey.json",
        "/etc/secrets/firebase-key.json",
        "/etc/secrets/firebase-adminsdk.json",
        "serviceAccountKey.json",
        "firebase-key.json"
    ] + glob.glob("*firebase-adminsdk*.json") + glob.glob("*.json")

    for path in search_paths:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if "service_account" in content and "private_key" in content:
                    creds_dict = json.loads(content)
                    cred = credentials.Certificate(creds_dict)
                    print(f"[Firebase] Loaded credentials from file: {path}")
                    break
            except Exception:
                continue

if cred:
    bucket_name = os.environ.get("FIREBASE_STORAGE_BUCKET", f"{cred.project_id}.appspot.com")
    try:
        firebase_app = firebase_admin.initialize_app(cred, {
            "storageBucket": bucket_name
        })
    except Exception:
        firebase_app = firebase_admin.get_app()

    try:
        firebase_db = firestore.client(app=firebase_app)
    except Exception:
        try:
            from google.cloud import firestore as g_firestore
            firebase_db = g_firestore.Client(project=cred.project_id, credentials=cred.get_credential())
        except Exception as e:
            print(f"[Firestore] Client init error: {e}")
            firebase_db = None

    try:
        firebase_bucket = storage.bucket(app=firebase_app)
    except Exception:
        firebase_bucket = None
    print(f"[Firebase] Connected to Cloud Firestore for project: {cred.project_id}")

def init_firestore():
    if not firebase_db:
        return
    try:
        # Check if any admin exists in Firestore
        admin_query = firebase_db.collection("users").where(filter=firestore.FieldFilter("role", "==", "admin")).limit(1).get()
        if not list(admin_query):
            firebase_db.collection("users").document("admin").set({
                "id": "admin",
                "email": "admin@bday.com",
                "password_hash": generate_password_hash("admin123"),
                "plain_password": "admin123",
                "role": "admin",
                "display_name": "Admin",
                "birthday_date": None,
                "created_at": firestore.SERVER_TIMESTAMP
            })
            print("[Firestore] Default Admin initialized (admin@bday.com / admin123).")
    except Exception as e:
        print(f"[Firestore] init_firestore error: {e}")

init_firestore()

# ----------------------------------------------------
# 3. PURE FIRESTORE DATA ACCESS METHODS
# ----------------------------------------------------

def db_get_user_by_email(email):
    if not email:
        return None
    email = email.strip().lower()
    if firebase_db:
        try:
            docs = firebase_db.collection("users").where(filter=firestore.FieldFilter("email", "==", email)).limit(1).get()
            for doc in docs:
                data = doc.to_dict()
                data["id"] = data.get("id") or doc.id
                return data
        except Exception as e:
            print(f"[Firestore] get_user_by_email error: {e}")
    # Default Admin fallback in memory
    if email == "admin@bday.com":
        return {
            "id": "admin",
            "email": "admin@bday.com",
            "password_hash": generate_password_hash("admin123"),
            "plain_password": "admin123",
            "role": "admin",
            "display_name": "Admin"
        }
    return None

def db_get_user_by_id(user_id):
    if not user_id:
        return None
    user_id_str = str(user_id)
    if firebase_db:
        try:
            doc = firebase_db.collection("users").document(user_id_str).get()
            if doc.exists:
                data = doc.to_dict()
                data["id"] = data.get("id") or doc.id
                return data
            docs = firebase_db.collection("users").where(filter=firestore.FieldFilter("id", "==", user_id)).limit(1).get()
            for d in docs:
                data = d.to_dict()
                data["id"] = data.get("id") or d.id
                return data
        except Exception as e:
            print(f"[Firestore] get_user_by_id error: {e}")
    if user_id_str == "admin":
        return {
            "id": "admin",
            "email": "admin@bday.com",
            "password_hash": generate_password_hash("admin123"),
            "plain_password": "admin123",
            "role": "admin",
            "display_name": "Admin"
        }
    return None

def db_get_all_standard_users():
    if firebase_db:
        try:
            docs = firebase_db.collection("users").stream()
            users_list = []
            for doc in docs:
                data = doc.to_dict()
                data["id"] = data.get("id") or doc.id
                if data.get("role") != "admin":
                    users_list.append(data)
            return sorted(users_list, key=lambda x: str(x.get("display_name", "")).lower())
        except Exception as e:
            print(f"[Firestore] get_all_standard_users error: {e}")
    return []

def db_create_user(display_name, email, password, birthday_date, lock_key="", profile_pic=None, scratch_reward=""):
    email = email.strip().lower()
    pass_hash = generate_password_hash(password)
    lock_hash = generate_password_hash(lock_key) if lock_key else None
    user_id = str(uuid.uuid4())[:8]
    
    user_data = {
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
        "scratch_reward": scratch_reward or "🎉 Congratulations! You unlocked a special birthday surprise and gift from all of us! 🎁",
        "created_at": firestore.SERVER_TIMESTAMP if firebase_db else str(datetime.now())
    }
    
    if firebase_db:
        try:
            firebase_db.collection("users").document(user_id).set(user_data)
            print(f"[Firestore] User '{display_name}' created in cloud!")
        except Exception as e:
            print(f"[Firestore] db_create_user error: {e}")
    return user_id

def db_delete_user(user_id):
    if not firebase_db:
        return
    try:
        user_id_str = str(user_id)
        firebase_db.collection("users").document(user_id_str).delete()
        for col in ["notes", "memories", "achievements"]:
            docs = firebase_db.collection(col).where(filter=firestore.FieldFilter("user_id", "==", user_id_str)).get()
            for d in docs:
                d.reference.delete()
    except Exception as e:
        print(f"[Firestore] db_delete_user error: {e}")

def db_update_user_profile_pic(user_id, photo_path):
    if not firebase_db:
        return
    try:
        user_id_str = str(user_id)
        firebase_db.collection("users").document(user_id_str).set({
            "profile_pic": photo_path,
            "updated_at": firestore.SERVER_TIMESTAMP
        }, merge=True)
    except Exception as e:
        print(f"[Firestore] update_user_profile_pic error: {e}")

def db_get_notes(user_id=None):
    if not firebase_db:
        return []
    try:
        if user_id:
            docs = firebase_db.collection("notes").where(filter=firestore.FieldFilter("user_id", "==", str(user_id))).get()
        else:
            docs = firebase_db.collection("notes").get()
            
        notes = []
        users_map = {u["id"]: u for u in db_get_all_standard_users()}
        for d in docs:
            data = d.to_dict()
            data["id"] = d.id
            u = users_map.get(data.get("user_id"))
            if u:
                data["recipient_name"] = u.get("display_name")
                data["recipient_email"] = u.get("email")
            created_val = data.get("created_at")
            if hasattr(created_val, "strftime"):
                data["created_at"] = created_val.strftime("%Y-%m-%d %H:%M")
            else:
                data["created_at"] = str(created_val or "")
            notes.append(data)
        return sorted(notes, key=lambda x: str(x.get("created_at", "")), reverse=True)
    except Exception as e:
        print(f"[Firestore] db_get_notes error: {e}")
        return []

def db_add_note(user_id, sender_name, title, message, icon="💌"):
    note_id = str(uuid.uuid4())[:8]
    if firebase_db:
        try:
            firebase_db.collection("notes").document(note_id).set({
                "id": note_id,
                "user_id": str(user_id),
                "sender_name": sender_name,
                "title": title,
                "message": message,
                "icon": icon,
                "created_at": firestore.SERVER_TIMESTAMP
            })
        except Exception as e:
            print(f"[Firestore] db_add_note error: {e}")
    return note_id

def db_delete_note(note_id):
    if firebase_db:
        try:
            firebase_db.collection("notes").document(str(note_id)).delete()
        except Exception as e:
            print(f"[Firestore] db_delete_note error: {e}")

def db_get_memories(user_id=None):
    if not firebase_db:
        return []
    try:
        if user_id:
            docs = firebase_db.collection("memories").where(filter=firestore.FieldFilter("user_id", "==", str(user_id))).get()
        else:
            docs = firebase_db.collection("memories").get()
            
        memories = []
        users_map = {u["id"]: u for u in db_get_all_standard_users()}
        for d in docs:
            data = d.to_dict()
            data["id"] = d.id
            u = users_map.get(data.get("user_id"))
            if u:
                data["recipient_name"] = u.get("display_name")
                data["recipient_email"] = u.get("email")
            created_val = data.get("created_at")
            if hasattr(created_val, "strftime"):
                data["created_at"] = created_val.strftime("%Y-%m-%d %H:%M")
            else:
                data["created_at"] = str(created_val or "")
            memories.append(data)
        return sorted(memories, key=lambda x: str(x.get("created_at", "")), reverse=True)
    except Exception as e:
        print(f"[Firestore] db_get_memories error: {e}")
        return []

def db_get_memory_by_id(memory_id):
    if not firebase_db:
        return None
    try:
        doc = firebase_db.collection("memories").document(str(memory_id)).get()
        if doc.exists:
            data = doc.to_dict()
            data["id"] = doc.id
            return data
    except Exception as e:
        print(f"[Firestore] db_get_memory_by_id error: {e}")
    return None

def db_add_memory(user_id, title, description, media_path, is_video=0, is_locked=0, lock_password=""):
    mem_id = str(uuid.uuid4())[:8]
    lock_hash = generate_password_hash(lock_password) if (is_locked and lock_password) else None
    if firebase_db:
        try:
            firebase_db.collection("memories").document(mem_id).set({
                "id": mem_id,
                "user_id": str(user_id),
                "title": title,
                "description": description,
                "media_path": media_path,
                "is_video": int(is_video),
                "is_locked": int(is_locked),
                "lock_password_hash": lock_hash,
                "created_at": firestore.SERVER_TIMESTAMP
            })
            db_earn_badge(user_id, "Memory Keeper")
        except Exception as e:
            print(f"[Firestore] db_add_memory error: {e}")
    return mem_id

def db_get_achievements(user_id):
    if not firebase_db:
        return []
    try:
        docs = firebase_db.collection("achievements").where(filter=firestore.FieldFilter("user_id", "==", str(user_id))).get()
        return [d.to_dict().get("badge_name") for d in docs]
    except Exception as e:
        print(f"[Firestore] db_get_achievements error: {e}")
        return []

def db_earn_badge(user_id, badge_name):
    if not firebase_db:
        return
    try:
        doc_id = f"{user_id}_{badge_name.replace(' ', '_')}"
        firebase_db.collection("achievements").document(doc_id).set({
            "id": doc_id,
            "user_id": str(user_id),
            "badge_name": badge_name,
            "earned_at": firestore.SERVER_TIMESTAMP
        }, merge=True)
    except Exception as e:
        print(f"[Firestore] db_earn_badge error: {e}")

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
    users = db_get_all_standard_users()
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
    
    current_user = db_get_user_by_id(session["user_id"])
    if current_user:
        session["birthday_date"] = current_user.get("birthday_date")
        session["display_name"] = current_user.get("display_name")
        
    badges = db_get_achievements(session["user_id"])
    notes_list = db_get_notes(session["user_id"])
    note_count = len(notes_list)
    recent_memories = db_get_memories(session["user_id"])[:3]
    
    nearest_bday, all_upcoming = None, []
    if session.get("user_role") == "admin":
        nearest_bday, all_upcoming = get_nearest_birthdays()
        
    return render_template(
        "dashboard.html", 
        earned_badges=badges, 
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
            
        user = db_get_user_by_email(email)
        
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
    if session.get("user_role") == "admin":
        notes_list = db_get_notes()
        users_list = db_get_all_standard_users()
        return render_template("notes.html", notes=notes_list, users=users_list)
    else:
        notes_list = db_get_notes(session["user_id"])
        return render_template("notes.html", notes=notes_list)

@app.route("/memories")
@birthday_required
def memories():
    if session.get("user_role") == "admin":
        memories_list = db_get_memories()
        users_list = db_get_all_standard_users()
        return render_template("memories.html", memories=memories_list, users=users_list)
    else:
        memories_list = db_get_memories(session["user_id"])
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
    
    db_update_user_profile_pic(target_user_id, photo_path)
    
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
        
    db_add_note(target_user_id, sender_name, title, message, icon)
    flash("Birthday Note & Letter added successfully! 💌", "success")
    return redirect(url_for("notes"))

@app.route("/api/delete-note/<string:note_id>", methods=["POST"])
@admin_required
def delete_note(note_id):
    db_delete_note(note_id)
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
        
    try:
        db_earn_badge(session["user_id"], badge_name)
        return jsonify({"status": "success", "message": f"Earned badge: {badge_name}"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

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
    
    db_add_memory(target_user_id, title, description, media_web_path, is_video, is_locked, lock_password)
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
        
    memory = db_get_memory_by_id(memory_id)
    if not memory:
        return jsonify({"status": "error", "message": "Memory not found"}), 404
        
    unlocked = False
    if memory.get("lock_password_hash") and check_password_hash(memory["lock_password_hash"], password):
        unlocked = True
    else:
        user_data = db_get_user_by_id(memory["user_id"])
        if user_data and user_data.get("lock_key_hash") and check_password_hash(user_data["lock_key_hash"], password):
            unlocked = True
            
    if unlocked:
        return jsonify({
            "status": "success",
            "media_path": memory["media_path"],
            "is_video": memory.get("is_video", 0),
            "description": memory.get("description", "")
        })
    else:
        return jsonify({"status": "error", "message": "Incorrect password"}), 403

# ----------------------------------------------------
# 7. ADMIN PANEL MANAGEMENT
# ----------------------------------------------------

@app.route("/admin")
@admin_required
def admin_panel():
    users_list = db_get_all_standard_users()
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
        
    existing = db_get_user_by_email(email)
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
        db_create_user(
            display_name=display_name,
            email=email,
            password=password,
            birthday_date=birthday_date,
            lock_key=user_lock_key,
            profile_pic=profile_pic,
            scratch_reward=scratch_reward
        )
        flash(f"Account for '{display_name}' created in Firebase! Saved permanently.", "success")
    except Exception as e:
        flash(f"Error creating user: {str(e)}", "error")
        
    return redirect(url_for("admin_panel"))

@app.route("/admin/delete-user/<string:user_id>", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    try:
        db_delete_user(user_id)
        flash("User account, memories, and notes revoked from Firebase.", "success")
    except Exception as e:
        flash(f"Error deleting user: {str(e)}", "error")
    finally:
        pass
        
    return redirect(url_for("admin_panel"))

@app.route("/admin/change-credentials", methods=["POST"])
@admin_required
def admin_change_credentials():
    new_email = request.form.get("new_email", "").strip().lower()
    new_password = request.form.get("new_password", "").strip()
    new_name = request.form.get("new_name", "").strip() or "Admin"
    
    if not new_email or not new_password:
        flash("Email and new password are required.", "error")
        return redirect(url_for("admin_panel"))
        
    admin_id = session.get("user_id", "admin")
    pass_hash = generate_password_hash(new_password)
    
    if firebase_db:
        try:
            firebase_db.collection("users").document(str(admin_id)).set({
                "id": str(admin_id),
                "email": new_email,
                "password_hash": pass_hash,
                "plain_password": new_password,
                "display_name": new_name,
                "role": "admin",
                "updated_at": firestore.SERVER_TIMESTAMP
            }, merge=True)
            
            session["user_email"] = new_email
            session["display_name"] = new_name
            flash("Admin credentials updated successfully! 🔑", "success")
        except Exception as e:
            flash(f"Error updating admin credentials: {str(e)}", "error")
            
    return redirect(url_for("admin_panel"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)


