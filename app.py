import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request, send_from_directory, abort
from flask_socketio import SocketIO, emit
from datetime import datetime
import sqlite3
import os
import uuid

app = Flask(__name__)
app.config["SECRET_KEY"] = "supersecretkey-chat-2025"

# Upload jusqu'à environ 1 Go côté Flask.
# Attention : si vous utilisez Nginx devant Flask, ajoutez aussi :
# client_max_body_size 1024M;
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="eventlet",
    max_http_buffer_size=MAX_UPLOAD_BYTES
)

CHAT_PASSWORD = "ikom26"
DB_PATH = os.path.join(os.path.dirname(__file__), "chat.db")
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

connected_users = {}  # sid -> username
typing_users = {}     # sid -> username


# ── DATABASE ──────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                username   TEXT    NOT NULL,
                text       TEXT    NOT NULL,
                time       TEXT    NOT NULL,
                msg_type   TEXT    NOT NULL DEFAULT 'text',
                file_id    TEXT,
                file_name  TEXT,
                file_size  INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME,
                deleted_at DATETIME
            )
        """)

        cols = [r[1] for r in db.execute("PRAGMA table_info(messages)").fetchall()]

        migrations = [
            ("msg_type", "TEXT NOT NULL DEFAULT 'text'"),
            ("file_id", "TEXT"),
            ("file_name", "TEXT"),
            ("file_size", "INTEGER"),
            ("updated_at", "DATETIME"),
            ("deleted_at", "DATETIME"),
        ]

        for col, definition in migrations:
            if col not in cols:
                db.execute(f"ALTER TABLE messages ADD COLUMN {col} {definition}")

        db.commit()

    print(f"[DB] SQLite ready -> {DB_PATH}")


def save_message(username, text, time, msg_type="text", file_id=None, file_name=None, file_size=None):
    with get_db() as db:
        cur = db.execute(
            """
            INSERT INTO messages
            (username, text, time, msg_type, file_id, file_name, file_size)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (username, text, time, msg_type, file_id, file_name, file_size)
        )
        db.commit()
        return cur.lastrowid


def load_history():
    """
    Sans limitation de nombre de messages.
    Tous les messages non supprimés sont chargés.
    """
    with get_db() as db:
        rows = db.execute("""
            SELECT
                id, username, text, time, msg_type,
                file_id, file_name, file_size,
                created_at, updated_at
            FROM messages
            WHERE deleted_at IS NULL
            ORDER BY id ASC
        """).fetchall()

    return [dict(r) for r in rows]


def get_message(message_id):
    with get_db() as db:
        row = db.execute("""
            SELECT
                id, username, text, time, msg_type,
                file_id, file_name, file_size,
                created_at, updated_at, deleted_at
            FROM messages
            WHERE id = ?
        """, (message_id,)).fetchone()

    return dict(row) if row else None


def update_message_text(message_id, username, new_text):
    with get_db() as db:
        cur = db.execute("""
            UPDATE messages
            SET text = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND username = ?
              AND msg_type = 'text'
              AND deleted_at IS NULL
        """, (new_text, message_id, username))

        if cur.rowcount == 0:
            db.rollback()
            return None

        db.commit()

    return get_message(message_id)


def soft_delete_message(message_id, username):
    msg = get_message(message_id)

    if not msg:
        return False, "Message introuvable."

    if msg["deleted_at"] is not None:
        return False, "Message déjà supprimé."

    if msg["username"] != username:
        return False, "Vous ne pouvez supprimer que vos propres messages."

    with get_db() as db:
        cur = db.execute("""
            UPDATE messages
            SET deleted_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND username = ?
              AND deleted_at IS NULL
        """, (message_id, username))

        if cur.rowcount == 0:
            db.rollback()
            return False, "Suppression impossible."

        db.commit()

    if msg["msg_type"] == "file" and msg["file_id"]:
        safe = os.path.basename(msg["file_id"])
        file_path = os.path.join(UPLOAD_DIR, safe)

        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
        except Exception as e:
            print(f"[WARN] Impossible de supprimer le fichier {file_path}: {e}")

    return True, "Message supprimé."


# ── ROUTES ───────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    username = request.headers.get("X-Username", "").strip()
    password = request.headers.get("X-Password", "").strip()

    if password != CHAT_PASSWORD or not username:
        abort(403)

    f = request.files.get("file")

    if not f:
        abort(400)

    original_name = os.path.basename(f.filename or "fichier")
    _, ext = os.path.splitext(original_name)

    # Extension nettoyée légèrement pour éviter les noms étranges côté disque.
    ext = "".join(c for c in ext if c.isalnum() or c == ".")[:30]

    file_id = str(uuid.uuid4()) + ext
    file_path = os.path.join(UPLOAD_DIR, file_id)

    f.save(file_path)
    file_size = os.path.getsize(file_path)

    now = datetime.now().strftime("%H:%M")

    message_id = save_message(
        username=username,
        text=f"📎 {original_name}",
        time=now,
        msg_type="file",
        file_id=file_id,
        file_name=original_name,
        file_size=file_size
    )

    msg = {
        "id": message_id,
        "username": username,
        "text": f"📎 {original_name}",
        "time": now,
        "msg_type": "file",
        "file_id": file_id,
        "file_name": original_name,
        "file_size": file_size,
        "updated_at": None,
    }

    socketio.emit("message", msg)
    return {"ok": True, "file_id": file_id, "message_id": message_id}


@app.route("/download/<file_id>")
def download(file_id):
    safe = os.path.basename(file_id)
    file_path = os.path.join(UPLOAD_DIR, safe)

    if not os.path.isfile(file_path):
        abort(404)

    with get_db() as db:
        row = db.execute("""
            SELECT file_name
            FROM messages
            WHERE file_id = ?
              AND deleted_at IS NULL
        """, (safe,)).fetchone()

    display_name = row["file_name"] if row else safe

    return send_from_directory(
        UPLOAD_DIR,
        safe,
        as_attachment=True,
        download_name=display_name
    )


# ── SOCKET EVENTS ────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    print(f"[+] {request.sid}")


@socketio.on("disconnect")
def on_disconnect():
    typing_users.pop(request.sid, None)
    emit("typing_update", {"users": list(typing_users.values())}, broadcast=True)

    if request.sid in connected_users:
        username = connected_users.pop(request.sid)
        emit(
            "user_left",
            {
                "username": username,
                "users": list(connected_users.values())
            },
            broadcast=True
        )
        print(f"[-] {username}")


@socketio.on("join")
def on_join(data):
    if data.get("password", "") != CHAT_PASSWORD:
        emit("auth_error", {"message": "Mot de passe incorrect ❌"})
        return

    username = (data.get("username", "") or "Anonyme").strip() or "Anonyme"
    connected_users[request.sid] = username

    emit("history", {"messages": load_history()})
    emit(
        "user_joined",
        {
            "username": username,
            "users": list(connected_users.values())
        },
        broadcast=True
    )

    print(f"[+] {username} joined")


@socketio.on("message")
def on_message(data):
    if request.sid not in connected_users:
        return

    username = connected_users[request.sid]

    text = data.get("text", "")

    if not isinstance(text, str):
        text = str(text)

    # Pas de limitation de texte.
    # On refuse seulement les messages entièrement vides.
    if not text.strip():
        return

    typing_users.pop(request.sid, None)
    emit("typing_update", {"users": list(typing_users.values())}, broadcast=True)

    now = datetime.now().strftime("%H:%M")
    message_id = save_message(username, text, now)

    emit(
        "message",
        {
            "id": message_id,
            "username": username,
            "text": text,
            "time": now,
            "msg_type": "text",
            "file_id": None,
            "file_name": None,
            "file_size": None,
            "updated_at": None,
        },
        broadcast=True
    )


@socketio.on("edit_message")
def on_edit_message(data):
    if request.sid not in connected_users:
        return

    username = connected_users[request.sid]

    try:
        message_id = int(data.get("id"))
    except Exception:
        emit("message_error", {"message": "Message invalide."})
        return

    new_text = data.get("text", "")

    if not isinstance(new_text, str):
        new_text = str(new_text)

    if not new_text.strip():
        emit("message_error", {"message": "Le message ne peut pas être vide."})
        return

    msg = get_message(message_id)

    if not msg:
        emit("message_error", {"message": "Message introuvable."})
        return

    if msg["username"] != username:
        emit("message_error", {"message": "Vous ne pouvez modifier que vos propres messages."})
        return

    if msg["msg_type"] != "text":
        emit("message_error", {"message": "Les fichiers ne peuvent pas être modifiés."})
        return

    updated = update_message_text(message_id, username, new_text)

    if not updated:
        emit("message_error", {"message": "Modification impossible."})
        return

    emit(
        "message_updated",
        {"message": updated},
        broadcast=True
    )


@socketio.on("delete_message")
def on_delete_message(data):
    if request.sid not in connected_users:
        return

    username = connected_users[request.sid]

    try:
        message_id = int(data.get("id"))
    except Exception:
        emit("message_error", {"message": "Message invalide."})
        return

    ok, message = soft_delete_message(message_id, username)

    if not ok:
        emit("message_error", {"message": message})
        return

    emit(
        "message_deleted",
        {"id": message_id},
        broadcast=True
    )


@socketio.on("typing")
def on_typing(data):
    if request.sid not in connected_users:
        return

    username = connected_users[request.sid]

    if data.get("typing"):
        typing_users[request.sid] = username
    else:
        typing_users.pop(request.sid, None)

    emit("typing_update", {"users": list(typing_users.values())}, broadcast=True)


if __name__ == "__main__":
    init_db()
    print("[*] Server on http://0.0.0.0:5000")
    socketio.run(
        app,
        debug=False,
        host="0.0.0.0",
        port=5000,
        use_reloader=False
    )