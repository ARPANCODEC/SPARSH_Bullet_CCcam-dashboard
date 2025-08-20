# app.py
# Single-file Flask dashboard — live stream, capture images with metadata, record MP4, gallery, CSV log
# With user authentication and admin/user roles
import os
import cv2
import csv
import time
import threading
from datetime import datetime
from flask import (
    Flask, render_template_string, Response,
    request, redirect, url_for, send_from_directory, send_file, flash, jsonify, session
)
from werkzeug.security import generate_password_hash, check_password_hash
import glob

app = Flask(__name__)
app.secret_key = "change-this-to-a-very-secret-key"  # Change this in production!

# ------------------- USER AUTHENTICATION -------------------
# Simple user storage (in production, use a database)
USERS_FILE = "users.csv"

# Initialize users file if it doesn't exist
if not os.path.exists(USERS_FILE):
    with open(USERS_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["username", "password_hash", "role", "registered_at"])
        # Create default admin user
        admin_hash = generate_password_hash("admin123")
        writer.writerow(["admin", admin_hash, "admin", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])

def load_users():
    users = {}
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                users[row['username']] = {
                    'password_hash': row['password_hash'],
                    'role': row['role'],
                    'registered_at': row['registered_at']
                }
    return users

def save_user(username, password_hash, role):
    users = load_users()
    if username in users:
        return False  # User already exists
    
    with open(USERS_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([username, password_hash, role, datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    
    return True

def delete_user(username):
    users = load_users()
    if username not in users:
        return False
    
    rows = []
    with open(USERS_FILE, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = [row for row in reader if row[0] != username]
    
    with open(USERS_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    
    return True

def is_logged_in():
    return 'username' in session

def is_admin():
    return is_logged_in() and session.get('role') == 'admin'

def login_required(f):
    def decorated_function(*args, **kwargs):
        if not is_logged_in():
            flash("Please log in to access this page.")
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

def admin_required(f):
    def decorated_function(*args, **kwargs):
        if not is_logged_in():
            flash("Please log in to access this page.")
            return redirect(url_for('login', next=request.url))
        if not is_admin():
            flash("Admin access required.")
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

# ------------------- CONFIG -------------------
POSSIBLE_STREAMS = [
    "rtsp://admin:admin123@192.168.128.10:554/avstream/channel=1/stream=1.sdp",
    "http://192.168.128.10/video/mjpg",
    "http://192.168.128.10/axis-cgi/mjpg/video.cgi"
]

CAPTURE_DIR = "captures/images"
VIDEO_DIR = "captures/videos"
CSV_PATH = "captures/records.csv"

os.makedirs(CAPTURE_DIR, exist_ok=True)
os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)

# CSV header: filename, reg_no, name, department, timestamp
if not os.path.exists(CSV_PATH):
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["filename", "reg_no", "name", "department", "timestamp"])

# ------------------- GLOBAL STATE -------------------
camera_stream = None
frame_lock = threading.Lock()
current_frame = None

frame_thread = None
frame_interval = 1/30  # Target 30 FPS for live feed

record_lock = threading.Lock()
recording = False
video_writer = None
current_video_filename = None

last_captured_filename = None

# ------------------- UTILITIES -------------------
def detect_camera_stream():
    for url in POSSIBLE_STREAMS:
        try:
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            if cap.isOpened():
                ret, frame = cap.read()
                cap.release()
                if ret and frame is not None:
                    app.logger.info(f"Detected stream: {url}")
                    return url
        except Exception as e:
            app.logger.warning(f"Error testing stream {url}: {e}")
            pass
    app.logger.warning("No camera stream detected from list.")
    return None

def test_custom_stream(stream_url):
    """Test if a custom stream URL is valid"""
    try:
        cap = cv2.VideoCapture(stream_url, cv2.CAP_FFMPEG)
        if cap.isOpened():
            ret, frame = cap.read()
            cap.release()
            return ret and frame is not None
    except Exception:
        pass
    return False

def start_frame_thread(stream_url):
    global frame_thread, camera_stream
    # Stop existing thread if running
    if frame_thread and frame_thread.is_alive():
        # We can't easily stop the thread, so we'll just update the camera_stream
        # and let the old thread eventually die when the stream fails
        pass
    
    camera_stream = stream_url
    frame_thread = threading.Thread(target=frame_loop, args=(stream_url,), daemon=True)
    frame_thread.start()
    app.logger.info(f"Started frame thread for: {stream_url}")

def frame_loop(stream_url):
    global current_frame, recording, video_writer
    cap = cv2.VideoCapture(stream_url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        app.logger.error(f"Unable to open camera stream: {stream_url}")
        return
    
    # Set buffer size to minimize latency
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    
    app.logger.info(f"Frame loop started for: {stream_url}")
    last_time = time.time()
    
    while camera_stream == stream_url:  # Only process if this is still the active stream
        current_time = time.time()
        elapsed = current_time - last_time
        
        # Only process frames at our target frame rate to reduce CPU usage
        if elapsed > frame_interval:
            ret, frame = cap.read()
            if not ret or frame is None:
                time.sleep(0.01)
                continue
            
            with frame_lock:
                current_frame = frame.copy()
                with record_lock:
                    if recording and video_writer is not None:
                        try:
                            video_writer.write(frame)
                        except Exception as e:
                            app.logger.error(f"VideoWriter error: {e}")
            
            last_time = current_time
        else:
            # Sleep for the remaining time to achieve target FPS
            time.sleep(max(0, frame_interval - (time.time() - current_time)))
    
    cap.release()
    app.logger.info(f"Frame loop ended for: {stream_url}")

def get_jpeg_bytes(quality=85):
    with frame_lock:
        if current_frame is None:
            return None
        frame = current_frame.copy()
    
    # Reduce image quality for faster transmission
    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
    ret, buf = cv2.imencode(".jpg", frame, encode_param)
    if not ret:
        return None
    return buf.tobytes()

def append_csv_row(filename, reg_no="", name="", department=""):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([filename, reg_no, name, department, ts])

def remove_from_csv(filename):
    """Remove all entries with the given filename from CSV"""
    rows = []
    with open(CSV_PATH, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = [row for row in reader if row[0] != filename]
    
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)

# ------------------- TEMPLATES -------------------
LOGIN_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Login - Camera Dashboard</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
  <style> 
    body { 
      background: #0b1220; 
      color: #e6eefc; 
      height: 100vh;
      display: flex;
      align-items: center;
    }
    .login-container {
      max-width: 400px;
      width: 100%;
      padding: 15px;
      margin: auto;
    }
  </style>
</head>
<body>
  <div class="login-container">
    <div class="card p-4">
      <h2 class="text-center mb-4">📹 Camera Dashboard</h2>
      
      {% with msgs = get_flashed_messages() %}
        {% if msgs %}
          {% for m in msgs %}
            <div class="alert alert-info">{{ m }}</div>
          {% endfor %}
        {% endif %}
      {% endwith %}
      
      <form method="POST" action="{{ url_for('login') }}">
        <input type="hidden" name="next" value="{{ request.args.get('next', '') }}">
        <div class="mb-3">
          <label for="username" class="form-label">Username</label>
          <input type="text" class="form-control" id="username" name="username" required>
        </div>
        <div class="mb-3">
          <label for="password" class="form-label">Password</label>
          <input type="password" class="form-control" id="password" name="password" required>
        </div>
        <button type="submit" class="btn btn-primary w-100">Login</button>
      </form>
      
      <div class="text-center mt-3">
        <a href="{{ url_for('register') }}">Register new account</a>
      </div>
      
      <div class="text-center mt-4 small text-muted">
        &copy; 2023 5G Lab. All Rights Reserved.<br>
        Made by Arpan Ari (arpancodec)
      </div>
    </div>
  </div>
</body>
</html>
"""

REGISTER_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Register - Camera Dashboard</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
  <style> 
    body { 
      background: #0b1220; 
      color: #e6eefc; 
      height: 100vh;
      display: flex;
      align-items: center;
    }
    .register-container {
      max-width: 400px;
      width: 100%;
      padding: 15px;
      margin: auto;
    }
  </style>
</head>
<body>
  <div class="register-container">
    <div class="card p-4">
      <h2 class="text-center mb-4">Register</h2>
      
      {% with msgs = get_flashed_messages() %}
        {% if msgs %}
          {% for m in msgs %}
            <div class="alert alert-info">{{ m }}</div>
          {% endfor %}
        {% endif %}
      {% endwith %}
      
      <form method="POST" action="{{ url_for('register') }}">
        <div class="mb-3">
          <label for="username" class="form-label">Username</label>
          <input type="text" class="form-control" id="username" name="username" required>
        </div>
        <div class="mb-3">
          <label for="password" class="form-label">Password</label>
          <input type="password" class="form-control" id="password" name="password" required>
        </div>
        <div class="mb-3">
          <label for="confirm_password" class="form-label">Confirm Password</label>
          <input type="password" class="form-control" id="confirm_password" name="confirm_password" required>
        </div>
        <button type="submit" class="btn btn-primary w-100">Register</button>
      </form>
      
      <div class="text-center mt-3">
        <a href="{{ url_for('login') }}">Back to Login</a>
      </div>
      
      <div class="text-center mt-4 small text-muted">
        &copy; 2023 5G Lab. All Rights Reserved.<br>
        Made by Arpan Ari (arpancodec)
      </div>
    </div>
  </div>
</body>
</html>
"""

ADMIN_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Admin Panel - Camera Dashboard</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
  <style> 
    body { 
      background: #0b1220; 
      color: #e6eefc; 
    }
    .card { 
      background: rgba(255,255,255,0.03); 
    }
  </style>
</head>
<body class="p-4">
  <div class="container">
    <div class="d-flex justify-content-between align-items-center mb-4">
      <h2>👑 Admin Panel</h2>
      <div>
        <a href="{{ url_for('index') }}" class="btn btn-secondary">Back to Dashboard</a>
        <a href="{{ url_for('logout') }}" class="btn btn-outline-light">Logout</a>
      </div>
    </div>

    {% with msgs = get_flashed_messages() %}
      {% if msgs %}
        {% for m in msgs %}
          <div class="alert alert-info">{{ m }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}

    <div class="row">
      <div class="col-md-6">
        <div class="card p-3 mb-4">
          <h4>User Management</h4>
          <table class="table table-dark table-striped">
            <thead>
              <tr>
                <th>Username</th>
                <th>Role</th>
                <th>Registered</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {% for user in users %}
                <tr>
                  <td>{{ user.username }}</td>
                  <td>{{ user.role }}</td>
                  <td>{{ user.registered_at }}</td>
                  <td>
                    {% if user.role != 'admin' %}
                      <form method="POST" action="{{ url_for('delete_user') }}" style="display:inline;">
                        <input type="hidden" name="username" value="{{ user.username }}">
                        <button type="submit" class="btn btn-sm btn-danger" onclick="return confirm('Are you sure you want to delete {{ user.username }}?')">Delete</button>
                      </form>
                    {% else %}
                      <span class="text-muted">Protected</span>
                    {% endif %}
                  </td>
                </tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </div>
      
      <div class="col-md-6">
        <div class="card p-3">
          <h4>System Information</h4>
          <div class="mb-3">
            <strong>Total Users:</strong> {{ users|length }}
          </div>
          <div class="mb-3">
            <strong>Admin Users:</strong> {{ admin_count }}
          </div>
          <div class="mb-3">
            <strong>Regular Users:</strong> {{ user_count }}
          </div>
          <div class="mb-3">
            <strong>Current Stream:</strong> {{ camera_stream or 'Not set' }}
          </div>
          <div class="mb-3">
            <strong>Recording Status:</strong> {{ 'Active' if recording else 'Inactive' }}
          </div>
        </div>
      </div>
    </div>
    
    <div class="text-center mt-4 small text-muted">
      &copy; 2023 5G Lab. All Rights Reserved. | Made by Arpan Ari (arpancodec)
    </div>
  </div>
</body>
</html>
"""

INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Camera Dashboard</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
  <style> body{ background:#0b1220; color:#e6eefc; } .card{ background: rgba(255,255,255,0.03); } </style>
</head>
<body class="p-4">
  <div class="container">
    <div class="d-flex justify-content-between mb-3">
      <h2>📹 Camera Dashboard</h2>
      <div>
        <span class="me-2">Logged in as <strong>{{ session.username }}</strong> ({{ session.role }})</span>
        {% if session.role == 'admin' %}
          <a href="{{ url_for('admin_panel') }}" class="btn btn-warning">Admin Panel</a>
        {% endif %}
        <a href="{{ url_for('gallery') }}" class="btn btn-info">Gallery</a>
        <a href="{{ url_for('logout') }}" class="btn btn-outline-light">Logout</a>
      </div>
    </div>

    {% with msgs = get_flashed_messages() %}
      {% if msgs %}
        {% for m in msgs %}
          <div class="alert alert-info">{{ m }}</div>
        {% endfor %}
        {% endif %}
      {% endwith %}

    <div class="card p-3 mb-3">
      <h5>Camera Stream Configuration</h5>
      <form method="POST" action="{{ url_for('set_stream') }}">
        <div class="row g-2">
          <div class="col-md-8">
            <input class="form-control" name="stream_url" placeholder="RTSP or HTTP stream URL" 
                   value="{{ stream_url or '' }}" required>
          </div>
          <div class="col-md-4">
            <button class="btn btn-primary w-100" type="submit">Set Stream</button>
          </div>
        </div>
      </form>
      <div class="mt-2">
        <form method="POST" action="{{ url_for('detect_stream') }}" style="display:inline;">
          <button class="btn btn-secondary">Auto Detect Stream</button>
        </form>
        <span class="ms-2 small text-muted">Current: {{ stream_url or 'Not set' }}</span>
      </div>
    </div>

    <div class="row">
      <div class="col-md-8">
        <div class="card p-2 mb-3">
          <h5>Live Feed</h5>
          {% if stream_url %}
            <img src="{{ url_for('video_feed') }}" class="img-fluid rounded" style="background:#000;">
          {% else %}
            <div class="text-center p-4" style="background:#000; color:#666;">
              No stream configured. Please set a stream URL above.
            </div>
          {% endif %}
        </div>

        <div class="card p-3">
          <h5>Capture Image (provide metadata)</h5>
          <form method="POST" action="{{ url_for('capture') }}">
            <div class="row g-2">
              <div class="col"><input class="form-control" name="reg_no" placeholder="Reg No" required></div>
              <div class="col"><input class="form-control" name="name" placeholder="Name" required></div>
              <div class="col"><input class="form-control" name="dept" placeholder="Department" required></div>
            </div>
            <div class="mt-3">
              <button class="btn btn-primary" type="submit" {{ 'disabled' if not stream_url }}>📸 Capture & Save</button>
            </div>
          </form>
        </div>
      </div>

      <div class="col-md-4">
        <div class="card p-3 mb-3">
          <h6>Recording</h6>
          <form method="POST" action="{{ url_for('start_record') }}" style="display:inline;">
            <button class="btn btn-success" {{ 'disabled' if not stream_url }}>⏺ Start Recording</button>
          </form>
          <form method="POST" action="{{ url_for('stop_record') }}" style="display:inline;">
            <button class="btn btn-danger" {{ 'disabled' if not recording }}>⏹ Stop Recording</button>
          </form>
          <div class="mt-3">
            <div><strong>Recording:</strong> {{ 'Yes' if recording else 'No' }}</div>
            <div><strong>Current video:</strong> {{ current_video or '-' }}</div>
          </div>
        </div>

        <div class="card p-3">
          <h6>Last Capture</h6>
          {% if last_image %}
            <img src="{{ url_for('get_image', filename=last_image) }}" class="img-fluid mb-2">
            <div class="small text-muted">{{ last_image }}</div>
          {% else %}
            <div class="small text-muted">None</div>
          {% endif %}
        </div>
      </div>
    </div>
    
    <div class="text-center mt-4 small text-muted">
      &copy; 2023 5G Lab. All Rights Reserved. | Made by Arpan Ari (arpancodec)
    </div>
  </div>
</body>
</html>
"""

GALLERY_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Gallery</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
  <script>
    function confirmDelete(filename, type) {
      if (confirm(`Are you sure you want to delete this ${type}: ${filename}?`)) {
        fetch('/delete/' + type + '/' + filename, { method: 'POST' })
          .then(response => response.json())
          .then(data => {
            if (data.success) {
              // Remove the element from the DOM
              const element = document.getElementById(`${type}-${filename}`);
              if (element) {
                element.remove();
              }
              // Show success message
              alert('File deleted successfully');
            } else {
              alert('Error deleting file: ' + data.message);
            }
          })
          .catch(error => {
            console.error('Error:', error);
            alert('Error deleting file');
          });
      }
    }
  </script>
</head>
<body class="p-4">
  <div class="container">
    <div class="d-flex justify-content-between mb-3">
      <h3>Gallery</h3>
      <div>
        <a href="{{ url_for('index') }}" class="btn btn-secondary">Back</a>
        <a href="{{ url_for('download_csv') }}" class="btn btn-success">Download CSV</a>
        <a href="{{ url_for('logout') }}" class="btn btn-outline-light">Logout</a>
      </div>
    </div>

    <h5>Images</h5>
    <div class="row">
      {% for img in images %}
        <div class="col-md-3 mb-3" id="image-{{ img }}">
          <div class="card">
            <img src="{{ url_for('get_image', filename=img) }}" class="card-img-top">
            <div class="card-body">
              <form method="POST" action="{{ url_for('save_metadata') }}">
                <input type="hidden" name="filename" value="{{ img }}">
                <input class="form-control mb-2" name="reg_no" placeholder="Reg No" required>
                <input class="form-control mb-2" name="name" placeholder="Name" required>
                <input class="form-control mb-2" name="department" placeholder="Department" required>
                <button class="btn btn-primary w-100" type="submit">Save Details</button>
              </form>
              <div class="d-flex gap-2 mt-2">
                <a href="{{ url_for('get_image', filename=img) }}" download class="btn btn-outline-secondary btn-sm flex-fill">Download</a>
                <button class="btn btn-outline-danger btn-sm" onclick="confirmDelete('{{ img }}', 'image')">Delete</button>
              </div>
            </div>
          </div>
        </div>
      {% endfor %}
    </div>

    <h5 class="mt-4">Videos</h5>
    <div class="list-group mb-4">
      {% for v in videos %}
        <div class="list-group-item d-flex justify-content-between align-items-center" id="video-{{ v }}">
          <div>{{ v }}</div>
          <div class="d-flex gap-2">
            <a href="{{ url_for('get_video', filename=v) }}" class="btn btn-outline-primary btn-sm" target="_blank">Play</a>
            <a href="{{ url_for('get_video', filename=v) }}" download class="btn btn-outline-secondary btn-sm">Download</a>
            <button class="btn btn-outline-danger btn-sm" onclick="confirmDelete('{{ v }}', 'video')">Delete</button>
          </div>
        </div>
      {% endfor %}
    </div>
    
    <div class="text-center mt-4 small text-muted">
      &copy; 2023 5G Lab. All Rights Reserved. | Made by Arpan Ari (arpancodec)
    </div>
  </div>
</body>
</html>
"""

# ------------------- AUTH ROUTES -------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if is_logged_in():
        return redirect(url_for('index'))
    
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        next_url = request.form.get("next", "").strip()
        
        users = load_users()
        if username in users and check_password_hash(users[username]['password_hash'], password):
            session['username'] = username
            session['role'] = users[username]['role']
            flash(f"Welcome back, {username}!")
            return redirect(next_url or url_for('index'))
        else:
            flash("Invalid username or password.")
    
    return render_template_string(LOGIN_HTML)

@app.route("/register", methods=["GET", "POST"])
def register():
    if is_logged_in():
        return redirect(url_for('index'))
    
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()
        
        if not username or not password:
            flash("Username and password are required.")
            return render_template_string(REGISTER_HTML)
        
        if password != confirm_password:
            flash("Passwords do not match.")
            return render_template_string(REGISTER_HTML)
        
        users = load_users()
        if username in users:
            flash("Username already exists.")
            return render_template_string(REGISTER_HTML)
        
        password_hash = generate_password_hash(password)
        if save_user(username, password_hash, "user"):
            flash("Registration successful. Please log in.")
            return redirect(url_for('login'))
        else:
            flash("Registration failed. Please try again.")
    
    return render_template_string(REGISTER_HTML)

@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.")
    return redirect(url_for('login'))

@app.route("/admin")
@admin_required
def admin_panel():
    users = []
    with open(USERS_FILE, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            users.append({
                'username': row['username'],
                'role': row['role'],
                'registered_at': row['registered_at']
            })
    
    admin_count = sum(1 for user in users if user['role'] == 'admin')
    user_count = sum(1 for user in users if user['role'] == 'user')
    
    return render_template_string(
        ADMIN_HTML, 
        users=users, 
        admin_count=admin_count, 
        user_count=user_count,
        camera_stream=camera_stream,
        recording=recording
    )

@app.route("/admin/delete_user", methods=["POST"])
@admin_required
def delete_user():
    username = request.form.get("username", "").strip()
    if not username:
        flash("Username is required.")
        return redirect(url_for('admin_panel'))
    
    if username == session.get('username'):
        flash("You cannot delete your own account.")
        return redirect(url_for('admin_panel'))
    
    users = load_users()
    if username not in users:
        flash("User not found.")
        return redirect(url_for('admin_panel'))
    
    if users[username]['role'] == 'admin':
        flash("Cannot delete admin users.")
        return redirect(url_for('admin_panel'))
    
    if delete_user(username):
        flash(f"User {username} deleted successfully.")
    else:
        flash(f"Failed to delete user {username}.")
    
    return redirect(url_for('admin_panel'))

# ------------------- APP ROUTES -------------------
@app.route("/")
@login_required
def index():
    return render_template_string(
        INDEX_HTML,
        stream_url=camera_stream,
        recording=recording,
        current_video=current_video_filename,
        last_image=last_captured_filename
    )

@app.route("/set_stream", methods=["POST"])
@login_required
def set_stream():
    stream_url = request.form.get("stream_url", "").strip()
    if not stream_url:
        flash("Please provide a stream URL")
        return redirect(url_for("index"))
    
    if test_custom_stream(stream_url):
        start_frame_thread(stream_url)
        flash(f"Stream set successfully: {stream_url}")
    else:
        flash(f"Unable to connect to stream: {stream_url}")
    
    return redirect(url_for("index"))

@app.route("/detect_stream", methods=["POST"])
@login_required
def detect_stream():
    detected_stream = detect_camera_stream()
    if detected_stream:
        start_frame_thread(detected_stream)
        flash(f"Auto-detected stream: {detected_stream}")
    else:
        flash("No stream could be auto-detected. Please enter a stream URL manually.")
    
    return redirect(url_for("index"))

@app.route("/video_feed")
@login_required
def video_feed():
    def gen():
        while True:
            jpg = get_jpeg_bytes(quality=80)  # Reduced quality for faster streaming
            if jpg is None:
                time.sleep(0.05)
                continue
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpg + b'\r\n')
            time.sleep(frame_interval)  # Control frame rate for live feed
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/capture", methods=["POST"])
@login_required
def capture():
    global last_captured_filename
    if not camera_stream:
        flash("No camera stream configured.")
        return redirect(url_for("index"))
        
    reg_no = request.form.get("reg_no", "").strip()
    name = request.form.get("name", "").strip()
    dept = request.form.get("dept", "").strip()

    with frame_lock:
        if current_frame is None:
            flash("No frame available to capture.")
            return redirect(url_for("index"))
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_reg = reg_no.replace(" ", "_") or "reg"
        safe_name = name.replace(" ", "_") or "name"
        safe_dept = dept.replace(" ", "_") or "dept"
        fname = f"{safe_reg}_{safe_name}_{safe_dept}_{ts}.jpg"
        path = os.path.join(CAPTURE_DIR, fname)
        cv2.imwrite(path, current_frame)
        last_captured_filename = fname
        append_csv_row(fname, reg_no, name, dept)

    flash(f"Captured: {fname}")
    return redirect(url_for("index"))

@app.route("/start_record", methods=["POST"])
@login_required
def start_record():
    global recording, video_writer, current_video_filename
    if not camera_stream:
        flash("No camera stream configured.")
        return redirect(url_for("index"))
        
    with frame_lock:
        if current_frame is None:
            flash("No frame available to start recording.")
            return redirect(url_for("index"))
        if recording:
            flash("Recording already in progress.")
            return redirect(url_for("index"))
        h, w = current_frame.shape[:2]
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        vname = f"record_{ts}.mp4"
        vpath = os.path.join(VIDEO_DIR, vname)
        
        # Use H.264 codec for better compatibility and performance
        fourcc = cv2.VideoWriter_fourcc(*'H264')
        
        # Reduce frame rate for recording to minimize lag
        video_writer = cv2.VideoWriter(vpath, fourcc, 15.0, (w, h))
        
        if not video_writer.isOpened():
            # Fallback to MP4V if H264 is not available
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            video_writer = cv2.VideoWriter(vpath, fourcc, 15.0, (w, h))
            
            if not video_writer.isOpened():
                video_writer = None
                flash("Failed to start recording.")
                return redirect(url_for("index"))
        
        with record_lock:
            recording = True
            current_video_filename = vname
    flash(f"Recording started: {vname}")
    return redirect(url_for("index"))

@app.route("/stop_record", methods=["POST"])
@login_required
def stop_record():
    global recording, video_writer, current_video_filename
    with record_lock:
        if not recording:
            flash("Recording is not active.")
            return redirect(url_for("index"))
        recording = False
        if video_writer:
            vname = current_video_filename
            video_writer.release()
            video_writer = None
            # Note: We do NOT add video entries to the CSV
            current_video_filename = None
            flash(f"Recording saved: {vname}")
        else:
            flash("No video writer to stop.")
    return redirect(url_for("index"))

@app.route("/gallery")
@login_required
def gallery():
    images = sorted(os.listdir(CAPTURE_DIR), reverse=True)
    videos = sorted(os.listdir(VIDEO_DIR), reverse=True)
    return render_template_string(GALLERY_HTML, images=images, videos=videos)

@app.route("/image/<path:filename>")
@login_required
def get_image(filename):
    return send_from_directory(CAPTURE_DIR, filename)

@app.route("/video/<path:filename>")
@login_required
def get_video(filename):
    return send_from_directory(VIDEO_DIR, filename)

@app.route("/save_metadata", methods=["POST"])
@login_required
def save_metadata():
    fname = request.form.get("filename")
    reg = request.form.get("reg_no", "").strip()
    name = request.form.get("name", "").strip()
    dept = request.form.get("department", "").strip()
    if not fname:
        flash("Missing filename.")
        return redirect(url_for("gallery"))
    append_csv_row(fname, reg, name, dept)
    flash(f"Saved metadata for {fname}")
    return redirect(url_for("gallery"))

@app.route("/delete/<file_type>/<filename>", methods=["POST"])
@login_required
def delete_file(file_type, filename):
    try:
        if file_type == "image":
            file_path = os.path.join(CAPTURE_DIR, filename)
            # Also remove from CSV
            remove_from_csv(filename)
        elif file_type == "video":
            file_path = os.path.join(VIDEO_DIR, filename)
        else:
            return jsonify({"success": False, "message": "Invalid file type"})
        
        if os.path.exists(file_path):
            os.remove(file_path)
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "message": "File not found"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route("/download_csv")
@login_required
def download_csv():
    return send_file(CSV_PATH, as_attachment=True)

# ------------------- STARTUP -------------------
if __name__ == "__main__":
    # Try to auto-detect on startup
    detected_stream = detect_camera_stream()
    if detected_stream:
        start_frame_thread(detected_stream)
        app.logger.info(f"Auto-detected stream on startup: {detected_stream}")
    else:
        app.logger.warning("No camera stream detected on startup; user will need to configure manually.")
    
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
