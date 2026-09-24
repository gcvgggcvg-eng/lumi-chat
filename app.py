"""
Nox Chat - Production Web Chat Application
"""
import os
import sqlite3
import uuid
import hashlib
import secrets
from datetime import datetime
from contextlib import contextmanager

from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['ADMIN_PASSWORD'] = os.environ.get('ADMIN_PASSWORD', 'NoxAdmin#2026!')
app.config['DATABASE'] = os.environ.get('DATABASE_PATH', 'noxchat.db')

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet', logger=False, engineio_logger=False)

def get_db():
    conn = sqlite3.connect(app.config['DATABASE'], check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

@contextmanager
def db_session():
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with db_session() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS rooms (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                name_en TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL,
                username TEXT NOT NULL,
                text TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                full_time TEXT NOT NULL,
                avatar_color TEXT,
                is_system INTEGER DEFAULT 0,
                FOREIGN KEY (room_id) REFERENCES rooms(id)
            );
            CREATE TABLE IF NOT EXISTS online_users (
                username TEXT PRIMARY KEY,
                sid TEXT,
                room_id TEXT,
                avatar_color TEXT,
                joined_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_messages_room ON messages(room_id);
            CREATE INDEX IF NOT EXISTS idx_messages_time ON messages(full_time);
        """)
        rooms = [
            ('public', 'الغرفة العامة', 'Public Room'),
            ('support', 'غرفة الدعم', 'Support Room'),
        ]
        for rid, name, name_en in rooms:
            conn.execute(
                "INSERT OR IGNORE INTO rooms (id, name, name_en) VALUES (?, ?, ?)",
                (rid, name, name_en)
            )

init_db()

sid_users = {}

def get_avatar_color(username: str) -> str:
    colors = [
        '#6366f1', '#8b5cf6', '#ec4899', '#f43f5e',
        '#f97316', '#eab308', '#22c55e', '#14b8a6',
        '#06b6d4', '#3b82f6'
    ]
    return colors[hash(username) % len(colors)]

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/admin')
def admin_page():
    return render_template('admin.html')

@app.route('/api/health')
def health():
    return jsonify({"status": "ok", "app": "Nox Chat"})

@app.route('/api/rooms')
def api_rooms():
    with db_session() as conn:
        rows = conn.execute("SELECT id, name, name_en FROM rooms").fetchall()
        result = []
        for r in rows:
            count = conn.execute(
                "SELECT COUNT(*) FROM online_users WHERE room_id = ?", (r['id'],)
            ).fetchone()[0]
            last = conn.execute(
                "SELECT username, text, timestamp FROM messages WHERE room_id = ? ORDER BY full_time DESC LIMIT 1",
                (r['id'],)
            ).fetchone()
            result.append({
                "id": r['id'],
                "name": r['name'],
                "name_en": r['name_en'],
                "user_count": count,
                "last_message": dict(last) if last else None
            })
    return jsonify(result)

@socketio.on('connect')
def on_connect():
    pass

@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    if sid in sid_users:
        user = sid_users.pop(sid)
        username = user['username']
        room_id = user.get('room')
        with db_session() as conn:
            conn.execute("DELETE FROM online_users WHERE username = ?", (username,))
        if room_id and room_id != 'admin_room':
            leave_room(room_id)
            emit('user_left', {
                'username': username,
                'room': room_id,
                'users_count': get_room_user_count(room_id)
            }, room=room_id)
        emit_admin_update()

def get_room_user_count(room_id):
    with db_session() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM online_users WHERE room_id = ?", (room_id,)
        ).fetchone()[0]

def get_room_users(room_id):
    with db_session() as conn:
        rows = conn.execute(
            "SELECT username FROM online_users WHERE room_id = ?", (room_id,)
        ).fetchall()
        return [r['username'] for r in rows]

@socketio.on('join')
def on_join(data):
    username = (data.get('username') or '').strip()
    room_id = data.get('room', 'public')
    if not username or len(username) < 2 or len(username) > 24:
        emit('error', {'message': 'اسم المستخدم يجب أن يكون بين 2 و 24 حرفاً'})
        return
    if room_id not in ('public', 'support'):
        room_id = 'public'

    with db_session() as conn:
        existing = conn.execute(
            "SELECT sid FROM online_users WHERE username = ?", (username,)
        ).fetchone()
        if existing:
            emit('error', {'message': 'هذا الاسم مستخدم حالياً، اختر اسماً آخر'})
            return
        color = get_avatar_color(username)
        conn.execute(
            "INSERT OR REPLACE INTO online_users (username, sid, room_id, avatar_color, joined_at) VALUES (?, ?, ?, ?, ?)",
            (username, request.sid, room_id, color, datetime.utcnow().isoformat())
        )

    sid_users[request.sid] = {
        'username': username,
        'room': room_id,
        'avatar_color': color,
        'is_admin': False
    }
    join_room(room_id)

    with db_session() as conn:
        msgs = conn.execute(
            "SELECT id, username, text, timestamp, full_time, avatar_color, is_system FROM messages WHERE room_id = ? ORDER BY full_time DESC LIMIT 80",
            (room_id,)
        ).fetchall()
        messages = [dict(m) for m in reversed(msgs)]
        room = conn.execute("SELECT name FROM rooms WHERE id = ?", (room_id,)).fetchone()

    emit('joined', {
        'username': username,
        'room': room_id,
        'room_name': room['name'] if room else room_id,
        'messages': messages,
        'users': get_room_users(room_id),
        'avatar_color': color
    })
    emit('user_joined', {
        'username': username,
        'room': room_id,
        'users_count': get_room_user_count(room_id),
        'users': get_room_users(room_id)
    }, room=room_id, include_self=False)
    emit_admin_update()

@socketio.on('switch_room')
def on_switch_room(data):
    sid = request.sid
    if sid not in sid_users:
        return
    user = sid_users[sid]
    old_room = user['room']
    new_room = data.get('room', 'public')
    if new_room not in ('public', 'support') or new_room == old_room:
        return

    username = user['username']
    leave_room(old_room)
    with db_session() as conn:
        conn.execute("UPDATE online_users SET room_id = ? WHERE username = ?", (new_room, username))
    join_room(new_room)
    user['room'] = new_room

    emit('user_left', {
        'username': username,
        'room': old_room,
        'users_count': get_room_user_count(old_room)
    }, room=old_room)

    with db_session() as conn:
        msgs = conn.execute(
            "SELECT id, username, text, timestamp, full_time, avatar_color, is_system FROM messages WHERE room_id = ? ORDER BY full_time DESC LIMIT 80",
            (new_room,)
        ).fetchall()
        messages = [dict(m) for m in reversed(msgs)]
        room = conn.execute("SELECT name FROM rooms WHERE id = ?", (new_room,)).fetchone()

    emit('joined', {
        'username': username,
        'room': new_room,
        'room_name': room['name'] if room else new_room,
        'messages': messages,
        'users': get_room_users(new_room),
        'avatar_color': user['avatar_color']
    })
    emit('user_joined', {
        'username': username,
        'room': new_room,
        'users_count': get_room_user_count(new_room),
        'users': get_room_users(new_room)
    }, room=new_room, include_self=False)
    emit_admin_update()

@socketio.on('send_message')
def on_send_message(data):
    sid = request.sid
    if sid not in sid_users:
        emit('error', {'message': 'يجب الانضمام أولاً'})
        return
    user = sid_users[sid]
    text = (data.get('text') or '').strip()
    if not text or len(text) > 1000:
        return

    msg_id = str(uuid.uuid4())
    now = datetime.utcnow()
    timestamp = now.strftime('%H:%M')
    full_time = now.isoformat()

    message = {
        'id': msg_id,
        'username': user['username'],
        'text': text,
        'timestamp': timestamp,
        'full_time': full_time,
        'avatar_color': user['avatar_color'],
        'is_system': 0
    }

    with db_session() as conn:
        conn.execute(
            "INSERT INTO messages (id, room_id, username, text, timestamp, full_time, avatar_color, is_system) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (msg_id, user['room'], user['username'], text, timestamp, full_time, user['avatar_color'])
        )
        conn.execute("""
            DELETE FROM messages WHERE room_id = ? AND id NOT IN (
                SELECT id FROM messages WHERE room_id = ? ORDER BY full_time DESC LIMIT 1000
            )
        """, (user['room'], user['room']))

    emit('new_message', message, room=user['room'])
    emit_admin_update()

@socketio.on('typing')
def on_typing(data):
    sid = request.sid
    if sid not in sid_users:
        return
    user = sid_users[sid]
    emit('user_typing', {
        'username': user['username'],
        'is_typing': bool(data.get('is_typing'))
    }, room=user['room'], include_self=False)

@socketio.on('join_admin')
def on_join_admin(data):
    password = data.get('password', '')
    if password != app.config['ADMIN_PASSWORD']:
        emit('error', {'message': 'كلمة المرور غير صحيحة'})
        return
    sid = request.sid
    sid_users[sid] = {
        'username': 'Admin',
        'room': 'admin_room',
        'avatar_color': '#6366f1',
        'is_admin': True
    }
    join_room('admin_room')
    emit('admin_joined', get_admin_payload())

@socketio.on('admin_clear_messages')
def on_admin_clear(data):
    sid = request.sid
    if sid not in sid_users or not sid_users[sid].get('is_admin'):
        return
    room_id = data.get('room')
    if room_id in ('public', 'support'):
        with db_session() as conn:
            conn.execute("DELETE FROM messages WHERE room_id = ?", (room_id,))
        emit('messages_cleared', {'room': room_id}, room=room_id)
        emit_admin_update()

@socketio.on('admin_broadcast')
def on_admin_broadcast(data):
    sid = request.sid
    if sid not in sid_users or not sid_users[sid].get('is_admin'):
        return
    text = (data.get('text') or '').strip()
    if not text:
        return
    now = datetime.utcnow()
    timestamp = now.strftime('%H:%M')
    full_time = now.isoformat()
    message = {
        'id': str(uuid.uuid4()),
        'username': 'النظام',
        'text': text,
        'timestamp': timestamp,
        'full_time': full_time,
        'avatar_color': '#6366f1',
        'is_system': 1
    }
    with db_session() as conn:
        for rid in ('public', 'support'):
            mid = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO messages (id, room_id, username, text, timestamp, full_time, avatar_color, is_system) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (mid, rid, 'النظام', text, timestamp, full_time, '#6366f1')
            )
            emit('new_message', {**message, 'id': mid}, room=rid)
    emit_admin_update()

def get_admin_payload():
    with db_session() as conn:
        rooms_info = []
        for rid in ('public', 'support'):
            room = conn.execute("SELECT name FROM rooms WHERE id = ?", (rid,)).fetchone()
            users = [r['username'] for r in conn.execute(
                "SELECT username FROM online_users WHERE room_id = ?", (rid,)
            ).fetchall()]
            msg_count = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE room_id = ?", (rid,)
            ).fetchone()[0]
            last_msgs = conn.execute(
                "SELECT username, text, timestamp FROM messages WHERE room_id = ? ORDER BY full_time DESC LIMIT 8",
                (rid,)
            ).fetchall()
            rooms_info.append({
                'id': rid,
                'name': room['name'] if room else rid,
                'user_count': len(users),
                'users': users,
                'message_count': msg_count,
                'last_messages': [dict(m) for m in reversed(last_msgs)]
            })
        online = [r['username'] for r in conn.execute("SELECT username FROM online_users").fetchall()]
    return {
        'rooms': rooms_info,
        'online_count': len(online),
        'online_users': online
    }

def emit_admin_update():
    emit('admin_update', get_admin_payload(), room='admin_room')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"Nox Chat running on port {port}")
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
