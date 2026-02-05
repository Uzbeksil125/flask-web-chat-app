from flask import Flask, render_template, request, session, redirect, abort
from flask_socketio import SocketIO, emit, join_room
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import os, json, uuid

# ================= CONFIG =================

app = Flask(__name__)
app.config['SECRET_KEY'] = 'dev-secret-change-me'
socketio = SocketIO(app, cors_allowed_origins="*")

USERS_FILE = 'data/users.json'
CHAT_DIR = 'data/chats'
UPLOAD_DIR = 'uploads'

os.makedirs(CHAT_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

connected = {}        # sid -> username
active_room = {}     # sid -> room

# ================= HELPERS =================

def now():
    return (datetime.utcnow() + timedelta(hours=5)).strftime('%H:%M')

def load(path, default):
    if not os.path.exists(path):
        return default
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def save(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def room_file(room):
    return os.path.join(CHAT_DIR, f"{room}.json")

def private_room(a, b):
    x, y = sorted([a, b])
    return f"private_{x}__{y}"

def normalize_user(u):
    u.setdefault('full_name', '')
    u.setdefault('blocked', [])
    u.setdefault('requests', [])
    u.setdefault('chats', [])
    return u

def validate_room(room, user):
    if room == 'global':
        return True
    return room.startswith('private_') and user in room

def inject_file(m):
    if m.get('type') == 'file' and 'file' in m:
        path = os.path.join(UPLOAD_DIR, m['file'])
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                m['data'] = f.read()


# ================= ROUTES =================

@app.route('/')
def index():
    return render_template('index.html', error=request.args.get('error'))

@app.route('/chat')
def chat():
    if 'username' not in session:
        return redirect('/?error=login_required')
    return render_template('chat.html', username=session['username'])

@app.route('/register', methods=['POST'])
def register():
    users = load(USERS_FILE, {})
    u = request.form.get('username','').lower()
    p = request.form.get('password','')

    if not u or not p:
        return redirect('/?error=missing_fields')
    if u in users:
        return redirect('/?error=user_exists')

    users[u] = normalize_user({
        'full_name': request.form.get('full_name',''),
        'password': generate_password_hash(p)
    })

    save(USERS_FILE, users)
    session['username'] = u
    return redirect('/chat')

@app.route('/login', methods=['POST'])
def login():
    users = load(USERS_FILE, {})
    u = request.form.get('username','').lower()
    p = request.form.get('password','')

    if u not in users:
        return redirect('/?error=invalid')

    users[u] = normalize_user(users[u])
    save(USERS_FILE, users)

    if not check_password_hash(users[u]['password'], p):
        return redirect('/?error=invalid')

    session['username'] = u
    return redirect('/chat')

# ================= SOCKET =================

@socketio.on('connect')
def connect():
    if 'username' not in session:
        return False

    u = session['username']
    connected[request.sid] = u
    active_room[request.sid] = 'global'

    join_room('global')
    join_room(f"user:{u}")

@socketio.on('join')
def join(data):
    user = connected.get(request.sid)
    room = data.get('room')

    if not validate_room(room, user):
        return

    active_room[request.sid] = room
    join_room(room)

    history = load(room_file(room), [])

    for m in history:
        m.setdefault('seen_by', [m.get('username')])
        inject_file(m)
        emit('message', m)

    emit('room_joined', {'room': room})

@socketio.on('message')
def message(data):
    user = connected.get(request.sid)
    room = data.get('room')
    text = data.get('msg','').strip()

    if not user or not text or not validate_room(room,user):
        return

    event = {
        'id': uuid.uuid4().hex,
        'type': 'text',
        'room': room,
        'username': user,
        'msg': text,
        'reply_to': data.get('reply_to'),
        'time': now(),
        'seen_by': [user]
    }

    hist = load(room_file(room), [])
    hist.append(event)
    save(room_file(room), hist)

    emit('message', event, room=room)

@socketio.on('image')
def image_msg(data):
    user = connected.get(request.sid)
    room = data.get('room')
    base64_data = data.get('image')

    if not user or not base64_data or not validate_room(room, user):
        return
    #need support for .jpg, .png, etc, but there is only support for png
    #vscode agent can you write it yourself?
    #vscode complete suggestor, do it yourself      
    
    filename = f"{uuid.uuid4().hex}.txt"
    with open(os.path.join(UPLOAD_DIR, filename),'w', encoding='utf-8') as f:
        f.write(base64_data)

    event = {
        'id': uuid.uuid4().hex,
        'type': 'image',
        'room': room,
        'username': user,
        'file': filename,
        'time': now(),
        'seen_by': [user]
    }

    hist = load(room_file(room), [])
    hist.append(event)
    save(room_file(room), hist)

    event['image'] = base64_data
    emit('message', event, room=room)

@socketio.on('read')
def read(data):
    user = connected.get(request.sid)
    room = data.get('room')

    if not validate_room(room,user):
        return

    hist = load(room_file(room), [])
    updated = False

    for m in hist:
        m.setdefault('seen_by', [m.get('username')])
        if user not in m['seen_by']:
            m['seen_by'].append(user)
            updated = True

    if updated:
        save(room_file(room), hist)
        emit('read', {'room':room,'user':user}, room=room)

# ----------- NOTIFICATIONS / CHATS -----------

@socketio.on('get_notifications')
def get_notifications():
    user = connected.get(request.sid)
    users = load(USERS_FILE,{})
    users[user] = normalize_user(users.get(user,{}))

    emit('notif_count', len(users[user]['requests']))
    for r in users[user]['requests']:
        emit('notification', {'from':r})

@socketio.on('chat_request')
def chat_request(data):
    sender = connected.get(request.sid)
    receiver = data.get('to','').lower()

    users = load(USERS_FILE,{})
    if receiver not in users or sender==receiver:
        return

    users[sender] = normalize_user(users[sender])
    users[receiver] = normalize_user(users[receiver])

    if sender not in users[receiver]['requests']:
        users[receiver]['requests'].append(sender)

    save(USERS_FILE, users)

    emit('notification', {'from':sender}, room=f"user:{receiver}")
    emit('notif_count', len(users[receiver]['requests']), room=f"user:{receiver}")

@socketio.on('accept_chat')
def accept_chat(data):
    a = connected.get(request.sid)
    b = data.get('from')

    room = private_room(a,b)

    users = load(USERS_FILE,{})
    users[a] = normalize_user(users[a])
    users[b] = normalize_user(users[b])

    if room not in users[a]['chats']:
        users[a]['chats'].append(room)
    if room not in users[b]['chats']:
        users[b]['chats'].append(room)

    if b in users[a]['requests']:
        users[a]['requests'].remove(b)

    save(USERS_FILE, users)

    emit('chat_added', {'room':room,'with':b}, room=f"user:{a}")
    emit('chat_added', {'room':room,'with':a}, room=f"user:{b}")

@socketio.on('get_chats')
def get_chats():
    user = connected.get(request.sid)
    users = load(USERS_FILE,{})
    users[user] = normalize_user(users.get(user,{}))
    emit('chat_list', users[user]['chats'])

# ================= MAIN =================
@socketio.on('file')
def file_msg(data):
    user = connected.get(request.sid)
    room = data.get('room')
    base64_data = data.get('data')
    filename = data.get('name')
    mime = data.get('mime')

    if not user or not base64_data or not validate_room(room, user):
        return

    ext = os.path.splitext(filename)[1]
    stored = f"{uuid.uuid4().hex}{ext}"

    with open(os.path.join(UPLOAD_DIR, stored), 'w', encoding='utf-8') as f:
        f.write(base64_data)

    event = {
        'id': uuid.uuid4().hex,
        'type': 'file',
        'room': room,
        'username': user,
        'file': stored,
        'name': filename,
        'mime': mime,
        'time': now(),
        'seen_by': [user]
    }

    hist = load(room_file(room), [])
    hist.append(event)
    save(room_file(room), hist)

    event['data'] = base64_data
    emit('message', event, room=room)

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
