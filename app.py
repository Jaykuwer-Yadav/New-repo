
import base64
import io
try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

def file_to_data_uri(file_storage):
    file_storage.seek(0)
    file_bytes = file_storage.read()
    file_storage.seek(0)
    
    filename = file_storage.filename.lower()
    
    # Compress images using Pillow to ensure payloads fit under Firestore 1MB document limit
    if HAS_PILLOW and any(filename.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp", ".heic"]):
        try:
            img = Image.open(io.BytesIO(file_bytes))
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
            out_buf = io.BytesIO()
            img.save(out_buf, format="JPEG", quality=80, optimize=True)
            comp_bytes = out_buf.getvalue()
            b64_str = base64.b64encode(comp_bytes).decode("utf-8")
            return f"data:image/jpeg;base64,{b64_str}"
        except Exception as e:
            print(f"[Image Compression Warning] {e}")

    if filename.endswith(".png"):
        mime = "image/png"
    elif filename.endswith(".gif"):
        mime = "image/gif"
    elif filename.endswith(".webp"):
        mime = "image/webp"
    elif filename.endswith(".mp4"):
        mime = "video/mp4"
    elif filename.endswith(".webm"):
        mime = "video/webm"
    elif filename.endswith(".mov"):
        mime = "video/quicktime"
    else:
        mime = "image/jpeg"
        
    b64_str = base64.b64encode(file_bytes).decode("utf-8")
    return f"data:{mime};base64,{b64_str}"


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

import requests
import google.auth
import google.auth.transport.requests
import firebase_admin
from firebase_admin import credentials, storage

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "birthday_memories_secret_key_for_sessions_2026")
app.config["UPLOAD_FOLDER"] = os.path.join("static", "uploads")
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024  # 512MB max upload limit

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs("templates", exist_ok=True)
os.makedirs(os.path.join("static", "css"), exist_ok=True)
os.makedirs(os.path.join("static", "js"), exist_ok=True)

# ----------------------------------------------------
# 1. PURE FIREBASE REST & CLOUD STORAGE INITIALIZATION
# ----------------------------------------------------
firebase_app = None
firebase_bucket = None
cred = None

# Check Environment Variables
for env_name in ["FIREBASE_CREDENTIALS_JSON", "FIREBASE_CREDENTIALS", "FIREBASE_KEY"]:
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

# Check Secret Files
if not cred:
    search_paths = [
        "/etc/secrets/serviceAccountKey.json",
        "/etc/secrets/firebase-key.json",
        "serviceAccountKey.json",
        "firebase-key.json"
    ] + glob.glob("*firebase-adminsdk*.json")

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
        firebase_app = firebase_admin.initialize_app(cred, {"storageBucket": bucket_name})
    except Exception:
        firebase_app = firebase_admin.get_app()
    try:
        firebase_bucket = storage.bucket(app=firebase_app)
    except Exception:
        firebase_bucket = None
    print(f"[Firebase] Connected to Cloud Firestore for project: {cred.project_id}")
else:
    print("[Firebase] WARNING: No credentials found.")

# ----------------------------------------------------
# 2. BULLETPROOF FIRESTORE REST ENGINE (ZERO gRPC BUGS)
# ----------------------------------------------------

_cached_token = None
_token_expiry = None

def get_auth_token():
    global _cached_token, _token_expiry
    if not cred:
        return None
    now = datetime.now()
    if _cached_token and _token_expiry and now < _token_expiry:
        return _cached_token
    try:
        g_cred = cred.get_credential()
        auth_req = google.auth.transport.requests.Request()
        g_cred.refresh(auth_req)
        _cached_token = g_cred.token
        _token_expiry = now + timedelta(minutes=50)
        return _cached_token
    except Exception as e:
        print(f"[Firestore REST] Auth token error: {e}")
        return None

def firestore_base_url():
    proj = cred.project_id if cred else "admyproperty-8a9d9"
    return f"https://firestore.googleapis.com/v1/projects/{proj}/databases/(default)/documents"

def firestore_to_dict(doc_json):
    if not doc_json:
        return {}
    fields = doc_json.get("fields", {})
    result = {}
    for k, v in fields.items():
        if "stringValue" in v:
            result[k] = v["stringValue"]
        elif "integerValue" in v:
            result[k] = int(v["integerValue"])
        elif "booleanValue" in v:
            result[k] = v["booleanValue"]
        elif "nullValue" in v:
            result[k] = None
        elif "timestampValue" in v:
            result[k] = v["timestampValue"]
    result["id"] = doc_json.get("name", "").split("/")[-1]
    return result

def dict_to_firestore(data_dict):
    fields = {}
    for k, v in data_dict.items():
        if v is None:
            fields[k] = {"nullValue": None}
        elif isinstance(v, bool):
            fields[k] = {"booleanValue": v}
        elif isinstance(v, int):
            fields[k] = {"integerValue": str(v)}
        else:
            fields[k] = {"stringValue": str(v)}
    return {"fields": fields}

def fs_get_all(collection):
    token = get_auth_token()
    if not token:
        return []
    try:
        url = f"{firestore_base_url()}/{collection}"
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            docs = resp.json().get("documents", [])
            return [firestore_to_dict(d) for d in docs]
        return []
    except Exception as e:
        print(f"[Firestore REST] fs_get_all error: {e}")
        return []

def fs_get_doc(collection, doc_id):
    token = get_auth_token()
    if not token:
        return None
    try:
        url = f"{firestore_base_url()}/{collection}/{doc_id}"
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return firestore_to_dict(resp.json())
        return None
    except Exception as e:
        print(f"[Firestore REST] fs_get_doc error: {e}")
        return None

def fs_set_doc(collection, doc_id, data_dict, merge=True):
    token = get_auth_token()
    if not token:
        return False
    try:
        url = f"{firestore_base_url()}/{collection}/{doc_id}"
        if merge and data_dict:
            mask_params = "&".join([f"updateMask.fieldPaths={k}" for k in data_dict.keys()])
            url = f"{url}?{mask_params}"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = dict_to_firestore(data_dict)
        resp = requests.patch(url, headers=headers, json=payload, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print(f"[Firestore REST] fs_set_doc error: {e}")
        return False

def fs_delete_doc(collection, doc_id):
    token = get_auth_token()
    if not token:
        return False
    try:
        url = f"{firestore_base_url()}/{collection}/{doc_id}"
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.delete(url, headers=headers, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print(f"[Firestore REST] fs_delete_doc error: {e}")
        return False

# ----------------------------------------------------
# 3. APPLICATION DATA ACCESS LAYER
# ----------------------------------------------------

def init_firestore_defaults():
    admin = fs_get_doc("users", "admin")
    if not admin:
        fs_set_doc("users", "admin", {
            "id": "admin",
            "email": "admin@bday.com",
            "password_hash": generate_password_hash("admin123"),
            "plain_password": "admin123",
            "role": "admin",
            "display_name": "Admin",
            "birthday_date": None
        })
        print("[Firestore REST] Default Admin created (admin@bday.com / admin123).")

init_firestore_defaults()

def db_get_user_by_email(email):
    if not email:
        return None
    email_clean = email.strip().lower()
    users = fs_get_all("users")
    for u in users:
        if (u.get("email") or "").strip().lower() == email_clean:
            return u
    if email_clean == "admin@bday.com":
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
    doc = fs_get_doc("users", user_id_str)
    if doc:
        return doc
    users = fs_get_all("users")
    for u in users:
        if str(u.get("id")) == user_id_str:
            return u
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
    users = fs_get_all("users")
    standard_users = [u for u in users if u.get("role") != "admin"]
    return sorted(standard_users, key=lambda x: str(x.get("display_name", "")).lower())

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
        "scratch_reward": scratch_reward or "ðŸŽ‰ Congratulations! You unlocked a special birthday surprise and gift from all of us! ðŸŽ",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M")
    }
    
    fs_set_doc("users", user_id, user_data)
    print(f"[Firestore REST] User '{display_name}' ({email}) saved permanently in cloud!")
    return user_id

def db_delete_user(user_id):
    user_id_str = str(user_id)
    fs_delete_doc("users", user_id_str)
    for col in ["notes", "memories", "achievements"]:
        items = fs_get_all(col)
        for item in items:
            if str(item.get("user_id")) == user_id_str:
                fs_delete_doc(col, item["id"])

def db_update_user_profile_pic(user_id, photo_path):
    user = db_get_user_by_id(user_id)
    if user:
        user["profile_pic"] = photo_path
        fs_set_doc("users", str(user["id"]), user)

def db_get_notes(user_id=None):
    notes = fs_get_all("notes")
    if user_id:
        notes = [n for n in notes if str(n.get("user_id")) == str(user_id)]
    users_map = {str(u["id"]): u for u in fs_get_all("users")}
    for n in notes:
        u = users_map.get(str(n.get("user_id")))
        if u:
            n["recipient_name"] = u.get("display_name")
            n["recipient_email"] = u.get("email")
    return sorted(notes, key=lambda x: str(x.get("created_at", "")), reverse=True)

def db_add_note(user_id, sender_name, title, message, icon="💌", is_locked=0, lock_password=""):
    note_id = str(uuid.uuid4())[:8]
    lock_hash = generate_password_hash(lock_password) if (is_locked and lock_password) else None
    fs_set_doc("notes", note_id, {
        "id": note_id,
        "user_id": str(user_id),
        "sender_name": sender_name,
        "title": title,
        "message": message,
        "icon": icon or "💌",
        "is_locked": int(is_locked),
        "lock_password_hash": lock_hash,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M")
    })
    return note_id

def db_delete_note(note_id):
    fs_delete_doc("notes", str(note_id))

def db_get_memories(user_id=None):
    memories = fs_get_all("memories")
    if user_id:
        memories = [m for m in memories if str(m.get("user_id")) == str(user_id)]
    users_map = {str(u["id"]): u for u in fs_get_all("users")}
    for m in memories:
        u = users_map.get(str(m.get("user_id")))
        if u:
            m["recipient_name"] = u.get("display_name")
            m["recipient_email"] = u.get("email")
    return sorted(memories, key=lambda x: str(x.get("created_at", "")), reverse=True)


def db_get_albums(user_id=None):
    memories = db_get_memories(user_id)
    albums = set()
    for m in memories:
        alb = m.get("album")
        if alb:
            albums.add(alb)
        else:
            albums.add("General Album")
    return sorted(list(albums))


def db_get_memory_by_id(memory_id):
    return fs_get_doc("memories", str(memory_id))

def db_add_memory(user_id, title, description, media_path, is_video=0, is_locked=0, lock_password="", album="General Album"):
    mem_id = str(uuid.uuid4())[:8]
    lock_hash = generate_password_hash(lock_password) if (is_locked and lock_password) else None
    clean_album = album.strip() if album and album.strip() else "General Album"
    fs_set_doc("memories", mem_id, {
        "id": mem_id,
        "user_id": str(user_id),
        "title": title,
        "description": description,
        "media_path": media_path,
        "album": clean_album,
        "is_video": int(is_video),
        "is_locked": int(is_locked),
        "lock_password_hash": lock_hash,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M")
    })
    db_earn_badge(user_id, "Memory Keeper")
    return mem_id

def db_get_achievements(user_id):
    achievements = fs_get_all("achievements")
    return [a.get("badge_name") for a in achievements if str(a.get("user_id")) == str(user_id)]

def db_earn_badge(user_id, badge_name):
    doc_id = f"{user_id}_{badge_name.replace(' ', '_')}"
    fs_set_doc("achievements", doc_id, {
        "id": doc_id,
        "user_id": str(user_id),
        "badge_name": badge_name,
        "earned_at": datetime.now().strftime("%Y-%m-%d %H:%M")
    })

# ----------------------------------------------------
# 4. HELPER FUNCTIONS & MIDDLEWARES
# ----------------------------------------------------

# ----------------------------------------------------
# GLOBAL APP SETTINGS (FIRESTORE)
# ----------------------------------------------------
def is_test_mode_unlocked():
    try:
        doc = fs_get_doc("settings", "app_config")
        if doc:
            return bool(doc.get("test_mode_unlocked", False))
    except Exception as e:
        print(f"[Settings] error: {e}")
    return False

def set_test_mode_unlocked(unlocked_bool):
    try:
        fs_set_doc("settings", "app_config", {"test_mode_unlocked": bool(unlocked_bool)})
    except Exception as e:
        print(f"[Settings] set error: {e}")

def is_logged_in():
    return "user_id" in session

def is_birthday_arrived():
    if session.get("user_role") == "admin":
        return True
    if is_test_mode_unlocked():
        return True
    bday_str = session.get("birthday_date")
    if not bday_str:
        return True
    try:
        clean_str = bday_str.strip()[:16]
        bdate = datetime.strptime(clean_str, "%Y-%m-%dT%H:%M")
        now = datetime.now()
        
        # 1. If TODAY is their birthday (same month and day) -> FULL 24-HOUR ACCESS!
        if now.month == bdate.month and now.day == bdate.day:
            return True
            
        # 2. If the user's initial setup date has arrived/passed
        target = bdate.replace(year=now.year)
        if now >= target:
            return True
            
        return False
    except Exception as e:
        print(f"[Birthday Lock Check Error] {e}")
        return True

def birthday_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_logged_in():
            return redirect(url_for("login"))
        if not is_birthday_arrived():
            flash("ðŸ”’ This section is locked! It will open when your birthday countdown reaches zero. â³", "info")
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

def get_next_annual_bday(bday_str):
    if not bday_str:
        return None
    try:
        now = datetime.now()
        clean_str = bday_str.strip()[:16]
        dt = datetime.strptime(clean_str, "%Y-%m-%dT%H:%M")
        
        # 1. If TODAY is the birthday date (same month & day) -> Celebrating TODAY!
        if now.month == dt.month and now.day == dt.day:
            return {
                "target_str": now.strftime("%Y-%m-%dT23:59:59"),
                "days_left": 0,
                "hours_left": 0,
                "diff_seconds": 0,
                "is_today": True
            }
            
        # 2. Otherwise calculate next upcoming birthday target
        target = dt.replace(year=now.year)
        if target < now:
            try:
                target = target.replace(year=now.year + 1)
            except ValueError:
                target = target.replace(year=now.year + 1, day=28)
        
        diff = target - now
        return {
            "target_str": target.strftime("%Y-%m-%dT%H:%M"),
            "days_left": diff.days,
            "hours_left": int((diff.total_seconds() % 86400) // 3600),
            "diff_seconds": max(0, diff.total_seconds()),
            "is_today": False
        }
    except Exception as e:
        print(f"[Birthday Calc Error] {e}")
        return None

def get_nearest_birthdays():
    users = db_get_all_standard_users()
    upcoming = []
    
    for u in users:
        bday_raw = u.get("birthday_date")
        if not bday_raw:
            continue
        info = get_next_annual_bday(bday_raw)
        if info:
            upcoming.append({
                "id": u["id"],
                "display_name": u.get("display_name") or u.get("email"),
                "email": u.get("email"),
                "birthday_date": info["target_str"],
                "original_birthday_date": bday_raw,
                "profile_pic": u.get("profile_pic"),
                "diff_seconds": info["diff_seconds"],
                "is_past": False,
                "days_left": info["days_left"],
                "hours_left": info["hours_left"]
            })
            
    sorted_upcoming = sorted(upcoming, key=lambda x: x["diff_seconds"])
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
        
    user_bday_info = get_next_annual_bday(session.get("birthday_date"))
    user_days_left = user_bday_info["days_left"] if user_bday_info else 0
    user_hours_left = user_bday_info["hours_left"] if user_bday_info else 0
    target_bday_str = user_bday_info["target_str"] if user_bday_info else session.get("birthday_date")

    return render_template(
        "dashboard.html", 
        earned_badges=badges, 
        birthday_date=target_bday_str,
        user_days_left=user_days_left,
        user_hours_left=user_hours_left,
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
            flash(f"Welcome back, {session['display_name']}! ðŸŽ‰", "success")
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
    user = db_get_user_by_id(session.get("user_id"))
    bday_str = user.get("birthday_date") if user else session.get("birthday_date")
    target_info = get_next_annual_bday(bday_str)
    
    bday_target = target_info.get("target_str") if target_info else ""
    is_today = target_info.get("is_today", False) if target_info else False
    
    return render_template("timer.html", birthday_date=bday_target, is_today=is_today)

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
    selected_album = request.args.get("album")
    if session.get("user_role") == "admin":
        selected_user_id = request.args.get("user_id")
        if selected_user_id:
            memories_list = db_get_memories(selected_user_id)
        else:
            memories_list = db_get_memories()
        users_list = db_get_all_standard_users()
        albums_list = db_get_albums()
        
        if selected_album and selected_album != "all":
            memories_list = [m for m in memories_list if (m.get("album") or "General Album") == selected_album]
            
        return render_template("memories.html", memories=memories_list, users=users_list, selected_user_id=selected_user_id, albums=albums_list, selected_album=selected_album)
    else:
        memories_list = db_get_memories(session["user_id"])
        albums_list = db_get_albums(session["user_id"])
        
        if selected_album and selected_album != "all":
            memories_list = [m for m in memories_list if (m.get("album") or "General Album") == selected_album]
            
        return render_template("memories.html", memories=memories_list, albums=albums_list, selected_album=selected_album)

@app.route("/api/upload-hero-photo", methods=["POST"])
@admin_required
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
        
    # Generate permanent Data URI for Firestore cloud database
    data_uri = file_to_data_uri(file)
    
    # Save profile_pic permanently in Cloud Firestore
    fs_set_doc("users", str(target_user_id), {"profile_pic": data_uri})
    
    flash("Hero photo updated permanently!", "success")
    return redirect(url_for(redirect_dest))

@app.route("/api/add-note", methods=["POST"])
@admin_required
def add_note():
    sender_name = request.form.get("sender_name", "").strip() or session.get("display_name", "Admin")
    title = request.form.get("title", "").strip()
    message = request.form.get("message", "").strip()
    icon = request.form.get("icon", "💌")
    target_user_id = request.form.get("user_id")
    is_locked = 1 if request.form.get("is_locked") == "on" else 0
    lock_password = request.form.get("lock_password", "").strip()
        
    if not title or not message or not target_user_id:
        flash("Title, recipient, and message content are required.", "error")
        return redirect(url_for("notes"))
        
    db_add_note(target_user_id, sender_name, title, message, icon, is_locked=is_locked, lock_password=lock_password)
    
    target_user = db_get_user_by_id(target_user_id)
    recipient_name = target_user.get("display_name", "User") if target_user else "User"
    
    if is_locked:
        flash(f"🔒 Secret locked note created successfully for {recipient_name}!", "success")
    else:
        flash(f"💌 Birthday note sent successfully to {recipient_name}!", "success")
        
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
    album = request.form.get("album", "General Album").strip() or "General Album"
    media_type = request.form.get("media_type", "auto")
    is_locked = 1 if request.form.get("is_locked") == "on" else 0
    lock_password = request.form.get("lock_password", "")
    target_user_id = request.form.get("user_id")
    
    if "media" not in request.files or not title or not target_user_id:
        flash("Title, recipient selection, and at least one media file are required.", "error")
        return redirect(url_for("memories"))
    
    files = request.files.getlist("media")
    valid_files = [f for f in files if f and f.filename and f.filename.strip()]
    
    if not valid_files:
        flash("No valid media files selected.", "error")
        return redirect(url_for("memories"))
        
    target_user = db_get_user_by_id(target_user_id)
    recipient_name = target_user.get("display_name", "Recipient") if target_user else "Recipient"

    uploaded_count = 0
    video_exts = (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".ogv")
    
    for idx, file in enumerate(valid_files, 1):
        filename_lower = file.filename.lower()
        if media_type == "video":
            is_vid = 1
        elif media_type == "image":
            is_vid = 0
        else:
            is_vid = 1 if filename_lower.endswith(video_exts) else 0

        if is_vid == 1:
            # Videos are saved to disk storage so Firestore document is tiny (<1KB) & avoids 1MB Firestore limit
            ext = os.path.splitext(file.filename)[1].lower() or ".mp4"
            unique_name = f"vid_{target_user_id}_{int(datetime.now().timestamp())}_{idx}{ext}"
            save_path = os.path.join(app.config["UPLOAD_FOLDER"], unique_name)
            file.save(save_path)
            media_path = f"/static/uploads/{unique_name}"
        else:
            # Photos are compressed and saved as permanent Base64 Data URIs
            media_path = file_to_data_uri(file)

        item_title = title if len(valid_files) == 1 else f"{title} ({idx})"

        db_add_memory(
            user_id=target_user_id,
            title=item_title,
            description=description,
            media_path=media_path,
            is_video=is_vid,
            is_locked=is_locked,
            lock_password=lock_password,
            album=album
        )
        uploaded_count += 1

    flash(f"Successfully uploaded {uploaded_count} memory item(s) to {recipient_name}'s '{album}' Album!", "success")
    return redirect(url_for("memories"))


@app.route("/api/delete-memory/<memory_id>", methods=["POST"])
@admin_required
def delete_memory(memory_id):
    try:
        mem = db_get_memory_by_id(memory_id)
        if mem:
            media_path = mem.get("media_path", "")
            if media_path and media_path.startswith("/static/"):
                local_path = media_path.lstrip("/")
                if os.path.exists(local_path):
                    try:
                        os.remove(local_path)
                    except Exception as e:
                        print(f"[Delete Memory File Error] {e}")
            fs_delete_doc("memories", str(memory_id))
            flash("Memory item deleted successfully!", "success")
        else:
            flash("Memory item not found.", "error")
    except Exception as e:
        flash(f"Error deleting memory: {str(e)}", "error")
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
    scratch_reward = request.form.get("scratch_reward", "").strip() or "ðŸŽ‰ Congratulations! You unlocked a special birthday surprise and gift from all of us! ðŸŽ"
    
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
    
    admin_data = {
        "id": str(admin_id),
        "email": new_email,
        "password_hash": pass_hash,
        "plain_password": new_password,
        "display_name": new_name,
        "role": "admin"
    }
    
    if fs_set_doc("users", str(admin_id), admin_data):
        session["user_email"] = new_email
        session["display_name"] = new_name
        flash("Admin credentials updated successfully! ðŸ”‘", "success")
    else:
        flash("Error updating admin credentials.", "error")
        
    return redirect(url_for("admin_panel"))

@app.route("/admin/toggle-test-mode", methods=["POST"])
@admin_required
def admin_toggle_test_mode():
    current = is_test_mode_unlocked()
    new_state = not current
    set_test_mode_unlocked(new_state)
    if new_state:
        flash("TEST MODE ACTIVATED: All birthday locks removed for testing!", "success")
    else:
        flash("STRICT BIRTHDAY LOCKS ENFORCED!", "info")
    return redirect(request.referrer or url_for("admin_panel"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)










@app.errorhandler(413)
def request_entity_too_large(error):
    flash("Upload failed: Total file size exceeds the maximum limit (512MB). Please upload fewer files at once or compress large videos.", "error")
    return redirect(request.referrer or url_for("memories"))


@app.errorhandler(500)
def internal_server_error(error):
    print(f"[500 Server Error Catch] {error}")
    flash("A temporary server glitch occurred. Please refresh the page.", "error")
    return redirect(request.referrer or url_for("index"))


@app.route("/api/unlock-note/<note_id>", methods=["POST"])
@birthday_required
def unlock_note(note_id):
    note = fs_get_doc("notes", str(note_id))
    if not note:
        return jsonify({"status": "error", "message": "Note not found."}), 404
        
    data = request.get_json(silent=True) or request.form
    password = data.get("password", "").strip()
    
    if session.get("user_role") == "admin":
        return jsonify({"status": "success", "message": note.get("message", "")})
        
    lock_hash = note.get("lock_password_hash")
    if lock_hash and check_password_hash(lock_hash, password):
        return jsonify({"status": "success", "message": note.get("message", "")})
    else:
        return jsonify({"status": "error", "message": "Incorrect secret password/PIN."}), 400


