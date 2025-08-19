# app.py
# -------------------------------------------------------
# Streamlit Offline NVR Dashboard for 8MP 5G Bullet Camera
# - Live feed from RTSP
# - Capture image + save metadata (Reg No, Name, Department)
# - Start/Stop MP4 recording
# - Simple gallery & downloads
# - Catchy UI with custom CSS
# -------------------------------------------------------
# Run:
#   pip install streamlit opencv-python pillow
#   streamlit run app.py
# -------------------------------------------------------

import streamlit as st
import cv2
import os
import time
import csv
from datetime import datetime
from threading import Thread, Event
from PIL import Image
import io
import base64

# =================== CONFIG ===================
st.set_page_config(
    page_title="Offline Camera Dashboard",
    page_icon="📹",
    layout="wide",
)

# ------------- Custom CSS -------------
CUSTOM_CSS = """
<style>
:root {
  --bg: #0b1220;
  --card: #101a2b;
  --accent: #6ee7ff;
  --accent2: #a78bfa;
  --muted: #99a3b3;
  --ok: #22c55e;
  --warn: #f59e0b;
  --danger: #ef4444;
}
* { font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; }
.stApp { background: radial-gradient(1200px 800px at 10% 10%, rgba(110,231,255,.08), transparent), 
                   radial-gradient(1000px 700px at 90% 20%, rgba(167,139,250,.08), transparent),
                   var(--bg); }
.big-hero {
  background: linear-gradient(135deg, rgba(110,231,255,.15), rgba(167,139,250,.15));
  border: 1px solid rgba(255,255,255,.1);
  padding: 22px 24px;
  border-radius: 18px;
  color: white;
  box-shadow: 0 10px 30px rgba(0,0,0,.25);
}
.hero-title {
  font-size: 28px; font-weight: 800; letter-spacing: .2px; margin: 0;
}
.hero-sub { color: var(--muted); margin-top: 6px; }
.card {
  background: var(--card);
  border: 1px solid rgba(255,255,255,.08);
  border-radius: 16px;
  padding: 14px;
  color: #e5eefc;
  box-shadow: 0 6px 20px rgba(0,0,0,.25);
}
.badge { padding: 6px 10px; border-radius: 999px; font-size: 12px; display: inline-block; }
.badge-ok { background: rgba(34,197,94,.15); color: var(--ok); border: 1px solid rgba(34,197,94,.35); }
.badge-warn { background: rgba(245,158,11,.15); color: var(--warn); border: 1px solid rgba(245,158,11,.35); }
.badge-danger { background: rgba(239,68,68,.15); color: var(--danger); border: 1px solid rgba(239,68,68,.35); }

button[kind="secondary"] { border-radius: 12px !important; }
.css-1v0mbdj a { color: var(--accent) !important; }
hr { border: none; height: 1px; background: linear-gradient(90deg, transparent, rgba(255,255,255,.2), transparent); }
.small { color: var(--muted); font-size: 12px; }
label, .stTextInput label, .stTextArea label { color: #cfe3ff !important; }
.stDownloadButton, .stButton button {
  border-radius: 12px !important;
  border: 1px solid rgba(255,255,255,.15) !important;
  background: linear-gradient(135deg, rgba(110,231,255,.18), rgba(167,139,250,.18)) !important;
  color: #ffffff !important;
}
.stButton button:hover {
  filter: brightness(1.1);
  box-shadow: 0 10px 25px rgba(110,231,255,.12), 0 10px 25px rgba(167,139,250,.12);
}
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# =================== PATHS & CSV ===================
VIDEO_DIR = "static/videos"
IMAGE_DIR = "static/images"
os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(IMAGE_DIR, exist_ok=True)
CSV_FILE = "metadata.csv"
if not os.path.exists(CSV_FILE):
    with open(CSV_FILE, "w", newline="") as f:
        csv.writer(f).writerow(["Image Filename", "Reg No", "Name", "Department", "Timestamp"])

# =================== CAMERA SETTINGS ===================
DEFAULT_RTSP = "rtsp://admin:admin123@192.168.128.10:554/avstream/channel=1/stream=1.sdp"
rtsp = st.sidebar.text_input("RTSP URL", value=DEFAULT_RTSP, help="Your camera stream URL")

# =================== SESSION STATE ===================
if "connected" not in st.session_state:
    st.session_state.connected = False
if "recording" not in st.session_state:
    st.session_state.recording = False
if "writer" not in st.session_state:
    st.session_state.writer = None
if "last_frame" not in st.session_state:
    st.session_state.last_frame = None
if "last_capture_path" not in st.session_state:
    st.session_state.last_capture_path = None
if "cap_thread" not in st.session_state:
    st.session_state.cap_thread = None
if "stop_event" not in st.session_state:
    st.session_state.stop_event = Event()
if "resolution" not in st.session_state:
    st.session_state.resolution = (1920, 1080)
if "rec_start" not in st.session_state:
    st.session_state.rec_start = None
if "status_msg" not in st.session_state:
    st.session_state.status_msg = "Disconnected"

# =================== CAMERA THREAD ===================
def reader_loop(rtsp_url: str):
    cap = cv2.VideoCapture(rtsp_url)
    if not cap.isOpened():
        st.session_state.connected = False
        st.session_state.status_msg = "Failed to open camera"
        return

    st.session_state.connected = True
    st.session_state.status_msg = "Live"
    # Try to read first frame to get resolution
    ok, frame = cap.read()
    if ok:
        h, w = frame.shape[:2]
        st.session_state.resolution = (w, h)
        st.session_state.last_frame = frame

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")

    while not st.session_state.stop_event.is_set():
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.08)
            continue

        st.session_state.last_frame = frame

        # Write recording
        if st.session_state.recording:
            if st.session_state.writer is None:
                fname = os.path.join(
                    VIDEO_DIR, f"video_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
                )
                w, h = st.session_state.resolution
                st.session_state.writer = cv2.VideoWriter(fname, fourcc, 20.0, (w, h))
                st.session_state.rec_start = time.time()
            st.session_state.writer.write(frame)
        else:
            if st.session_state.writer is not None:
                st.session_state.writer.release()
                st.session_state.writer = None
                st.session_state.rec_start = None

    cap.release()
    if st.session_state.writer is not None:
        st.session_state.writer.release()
        st.session_state.writer = None

# =================== HEADER ===================
st.markdown(
    """
<div class="big-hero">
  <div class="hero-title">📹 Offline Camera Dashboard</div>
  <div class="hero-sub">Live view • Instant snapshots • MP4 recording • On-device storage</div>
</div>
<br/>
""",
    unsafe_allow_html=True,
)

# =================== SIDEBAR ===================
with st.sidebar:
    st.markdown("### Connection")
    c1, c2 = st.columns([1,1])
    connect_btn = c1.button("🔌 Connect", use_container_width=True)
    stop_btn = c2.button("🛑 Disconnect", use_container_width=True)

    if connect_btn and (st.session_state.cap_thread is None or not st.session_state.cap_thread.is_alive()):
        st.session_state.stop_event.clear()
        st.session_state.cap_thread = Thread(target=reader_loop, args=(rtsp,), daemon=True)
        st.session_state.cap_thread.start()
        time.sleep(0.3)

    if stop_btn and st.session_state.cap_thread is not None:
        st.session_state.stop_event.set()
        st.session_state.status_msg = "Disconnected"

    st.markdown("---")
    st.markdown("### Status")
    badge_class = "badge-ok" if st.session_state.connected else "badge-danger"
    st.markdown(f'<span class="badge {badge_class}"> {st.session_state.status_msg} </span>', unsafe_allow_html=True)
    if st.session_state.rec_start:
        elapsed = int(time.time() - st.session_state.rec_start)
        st.markdown(f"**Recording:** {elapsed} s")
    w, h = st.session_state.resolution
    st.caption(f"Resolution: {w}×{h}")

    st.markdown("---")
    st.markdown("### Storage")
    def folder_size(p):
        total = 0
        for root, _, files in os.walk(p):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except:  # noqa: E722
                    pass
        return total
    def human(n):
        for u in ["B","KB","MB","GB","TB"]:
            if n < 1024: return f"{n:.1f} {u}"
            n /= 1024
    st.write(f"Images: **{human(folder_size(IMAGE_DIR))}**")
    st.write(f"Videos: **{human(folder_size(VIDEO_DIR))}**")

# =================== TABS ===================
tab_live, tab_gallery, tab_about = st.tabs(["🎬 Live View", "🗂️ Gallery", "ℹ️ About"])

# ----------- LIVE VIEW TAB -----------
with tab_live:
    colA, colB = st.columns([7, 5], gap="large")

    # Live feed
    with colA:
        st.markdown("#### Live Feed")
        live_container = st.empty()
        # Render latest frame (auto-refresh via Streamlit reruns)
        frame = st.session_state.last_frame
        if frame is not None:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            live_container.image(rgb, channels="RGB", use_column_width=True, caption="Live")
        else:
            st.info("Connect to the camera to see the live feed.")

    with colB:
        st.markdown("#### Controls")
        c1, c2 = st.columns(2)
        # Recording toggle
        if c1.toggle("⏺️ Record MP4", value=st.session_state.recording, key="rec_toggle"):
            if not st.session_state.recording:
                # switching ON
                st.session_state.recording = True
                st.toast("Recording started…", icon="⏺️")
            else:
                # already ON, nothing
                pass
        else:
            if st.session_state.recording:
                # switching OFF
                st.session_state.recording = False
                st.toast("Recording stopped.", icon="🛑")

        # Capture image
        if c2.button("📸 Capture Still", use_container_width=True):
            f = st.session_state.last_frame
            if f is not None:
                fname = f"img_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
                fpath = os.path.join(IMAGE_DIR, fname)
                cv2.imwrite(fpath, f)
                st.session_state.last_capture_path = fpath
                st.success(f"Captured: {fname}")
            else:
                st.error("No frame available to capture.")

        st.markdown("---")
        st.markdown("#### Save Image Metadata")
        if st.session_state.last_capture_path:
            # Preview + form
            img_bgr = cv2.imread(st.session_state.last_capture_path)
            if img_bgr is not None:
                img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
                st.image(img_rgb, caption=os.path.basename(st.session_state.last_capture_path), use_column_width=True)
            c3, c4 = st.columns(2)
            with c3:
                reg_no = st.text_input("Registration No", key="reg_no")
                name = st.text_input("Name", key="name")
            with c4:
                dept = st.text_input("Department", key="department")
                # Download button for the captured image
                with open(st.session_state.last_capture_path, "rb") as f:
                    st.download_button("⬇️ Download Image", f, file_name=os.path.basename(st.session_state.last_capture_path), use_container_width=True)

            if st.button("💾 Save Metadata", use_container_width=True):
                with open(CSV_FILE, "a", newline="") as f:
                    csv.writer(f).writerow([
                        os.path.basename(st.session_state.last_capture_path),
                        reg_no, name, dept,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    ])
                st.success("Metadata saved.")
                # Clear inputs for next entry
                st.session_state.reg_no = ""
                st.session_state.name = ""
                st.session_state.department = ""
        else:
            st.caption("Capture an image to enter metadata.")

# ----------- GALLERY TAB -----------
with tab_gallery:
    st.markdown("#### Recent Captures")
    imgs = sorted([f for f in os.listdir(IMAGE_DIR) if f.lower().endswith((".jpg",".png",".jpeg"))], reverse=True)[:12]
    if imgs:
        cols = st.columns(4)
        for i, fn in enumerate(imgs):
            path = os.path.join(IMAGE_DIR, fn)
            with cols[i % 4]:
                st.markdown(f"<div class='card'><b>{fn}</b><br/><span class='small'>{os.path.getsize(path)/1024:.0f} KB</span></div>", unsafe_allow_html=True)
                image = Image.open(path).convert("RGB")
                st.image(image, use_column_width=True)
                with open(path, "rb") as f:
                    st.download_button("Download", f, file_name=fn, key=f"dl_img_{fn}", use_container_width=True)
    else:
        st.info("No images yet. Capture from Live View.")

    st.markdown("---")
    st.markdown("#### Recent Videos")
    vids = sorted([f for f in os.listdir(VIDEO_DIR) if f.lower().endswith(".mp4")], reverse=True)[:12]
    if vids:
        vcols = st.columns(3)
        for i, fn in enumerate(vids):
            path = os.path.join(VIDEO_DIR, fn)
            with vcols[i % 3]:
                st.markdown(f"<div class='card'><b>{fn}</b><br/><span class='small'>{os.path.getsize(path)/1024/1024:.2f} MB</span></div>", unsafe_allow_html=True)
                with open(path, "rb") as f:
                    st.download_button("Download MP4", f, file_name=fn, key=f"dl_vid_{fn}", use_container_width=True)
    else:
        st.info("No recordings yet. Toggle Record in Live View.")

    st.markdown("---")
    st.markdown("#### Metadata Table")
    if os.path.exists(CSV_FILE):
        import pandas as pd
        df = pd.read_csv(CSV_FILE)
        if not df.empty:
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.caption("No metadata saved yet.")
    else:
        st.caption("No metadata file found yet.")

# ----------- ABOUT TAB -----------
with tab_about:
    st.markdown("#### About")
    st.write(
        """
- **Offline**: Works point-to-point via Ethernet, no Internet required.  
- **Live View**: RTSP feed rendered as images in near real-time.  
- **Recording**: H.264/H.265 camera streams → saved as `.mp4` using `cv2.VideoWriter('mp4v')`.  
- **Snapshots**: Saves to `static/images/` and logs metadata in `metadata.csv`.  
- **Tip**: Prefer H.265 in camera settings to reduce bitrate at 8MP.
        """
    )
    st.markdown("---")
    st.write("**Folders**")
    st.code(f"Images  → {os.path.abspath(IMAGE_DIR)}\nVideos  → {os.path.abspath(VIDEO_DIR)}\nCSV     → {os.path.abspath(CSV_FILE)}")
    st.caption("If you change RTSP URL in the sidebar, click **Disconnect** then **Connect**.")

# =================== AUTO-REFRESH NOTE ===================
# Streamlit re-runs the script on UI interaction. The reader thread keeps frames fresh.
# No sleep loop required here; UI updates when user interacts or via internal refresh cadence.
# =================== END OF APP ===================