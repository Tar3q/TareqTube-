import os
import json
import threading
import uuid
import subprocess
import re
from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO
import yt_dlp

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, cors_allowed_origins="*")

DB_FILE = 'database.json'
active_downloads = {}


def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    with open(DB_FILE, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except:
            return {}


def save_db(db):
    with open(DB_FILE, 'w', encoding='utf-8') as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def resolve_video_file(file_path, save_path=None, title=None):
    """Robustly resolve the actual video file on disk even if temporary or extension differed."""
    if file_path and os.path.isfile(file_path):
        return os.path.normpath(file_path)

    candidates = []
    if file_path:
        norm = os.path.normpath(file_path)
        candidates.append(norm)
        # Strip temporary format tags like .f251.webm or .f616.mp4
        stripped = re.sub(r'\.f[a-zA-Z0-9_\-]+(?=\.[a-zA-Z0-9]+$|\.part$)', '', norm)
        stripped = re.sub(r'\.part$', '', stripped)
        candidates.append(stripped)

        base, _ = os.path.splitext(stripped)
        for ext in ['.mp4', '.mkv', '.webm', '.mp3', '.m4a', '.opus']:
            candidates.append(base + ext)
            candidates.append(os.path.splitext(norm)[0] + ext)

    for c in candidates:
        if c and os.path.isfile(c):
            return os.path.normpath(c)

    # Search in directory
    dir_to_check = save_path or (os.path.dirname(file_path) if file_path else None)
    if dir_to_check and os.path.isdir(dir_to_check):
        clean_title = re.sub(r'[^\w\s\u0600-\u06FF]', '', title or '').strip().lower()
        matches = []
        try:
            for root, dirs, files in os.walk(dir_to_check):
                for f in files:
                    if f.endswith('.part'):
                        continue
                    full = os.path.join(root, f)
                    f_clean = re.sub(r'[^\w\s\u0600-\u06FF]', '', f).lower()
                    if clean_title and len(clean_title) >= 3 and (clean_title in f_clean or f_clean in clean_title):
                        matches.append((full, os.path.getmtime(full)))
                break  # Top level of dir
            if matches:
                matches.sort(key=lambda x: x[1], reverse=True)
                return os.path.normpath(matches[0][0])
        except Exception:
            pass

    return None


class DownloadInterrupted(Exception):
    pass


class MyLogger(object):
    def __init__(self, sid, dl_id):
        self.sid = sid
        self.dl_id = dl_id

    def debug(self, msg):
        if not msg.startswith('[download]') or 'ETA' not in msg:
            socketio.emit('log', {'msg': msg, 'id': self.dl_id}, to=self.sid)

    def warning(self, msg):
        socketio.emit('log', {'msg': f"تحذير: {msg}", 'id': self.dl_id}, to=self.sid)

    def error(self, msg):
        socketio.emit('log', {'msg': f"خطأ: {msg}", 'id': self.dl_id}, to=self.sid)


def progress_hook(d, sid, dl_id):
    if dl_id in active_downloads:
        if active_downloads[dl_id].get('pause'):
            raise DownloadInterrupted("Paused")
        if active_downloads[dl_id].get('cancel'):
            raise DownloadInterrupted("Cancelled")

    db = load_db()
    if dl_id not in db:
        return

    if d['status'] == 'downloading':
        try:
            percent_str = d.get('_percent_str', '0%').strip('\x1b[0;94m\x1b[0m').strip()
            speed_str = d.get('_speed_str', 'N/A').strip('\x1b[0;32m\x1b[0m').strip()
            eta_str = d.get('_eta_str', 'N/A').strip('\x1b[0;33m\x1b[0m').strip()
            filename = d.get('filename', '')

            db[dl_id]['percent'] = percent_str
            # Only save filename if not a temp stream (e.g. .f251.webm)
            if filename and not re.search(r'\.f[a-zA-Z0-9_\-]+\.', filename):
                db[dl_id]['filename'] = filename
            save_db(db)

            socketio.emit('progress', {
                'id': dl_id,
                'percent': percent_str,
                'speed': speed_str,
                'eta': eta_str,
                'filename': filename
            }, to=sid)
        except Exception:
            pass
    elif d['status'] == 'finished':
        filename = d.get('filename', '')
        if filename and not re.search(r'\.f[a-zA-Z0-9_\-]+\.', filename):
            db[dl_id]['filename'] = filename
            save_db(db)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/favicon.ico')
def favicon():
    return send_file(os.path.join(app.root_path, 'static', 'favicon.ico'), mimetype='image/vnd.microsoft.icon')


@app.route('/api/history', methods=['GET'])
def get_history():
    return jsonify(load_db())


@app.route('/api/info', methods=['POST'])
def get_info():
    data = request.json
    url = data.get('url')
    if not url:
        return jsonify({'error': 'الرابط مطلوب'}), 400

    ydl_opts = {
        'extract_flat': 'in_playlist',
        'quiet': True,
        'no_warnings': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url, download=False)

            if 'entries' in info_dict:
                entries = list(info_dict.get('entries', []))
                videos = []
                for e in entries[:50]:  # limit preview to 50
                    if e:
                        vid_id = e.get('id', '')
                        videos.append({
                            'id': vid_id,
                            'title': e.get('title', 'بدون عنوان'),
                            'thumbnail': e.get('thumbnail') or (f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg" if vid_id else ''),
                            'duration': e.get('duration', 0),
                            'url': e.get('url') or e.get('webpage_url') or f"https://www.youtube.com/watch?v={vid_id}"
                        })
                return jsonify({
                    'title': info_dict.get('title', 'قائمة تشغيل'),
                    'count': len(entries),
                    'is_playlist': True,
                    'thumbnail': info_dict.get('thumbnail', ''),
                    'videos': videos
                })
            else:
                vid_id = info_dict.get('id', '')
                thumb = info_dict.get('thumbnail', '') or (f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg" if vid_id else '')
                return jsonify({
                    'title': info_dict.get('title', 'فيديو'),
                    'count': 1,
                    'is_playlist': False,
                    'thumbnail': thumb,
                    'duration': info_dict.get('duration', 0),
                    'videos': [{
                        'id': vid_id,
                        'title': info_dict.get('title', 'فيديو'),
                        'thumbnail': thumb,
                        'duration': info_dict.get('duration', 0),
                        'url': url
                    }]
                })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/play', methods=['POST'])
def play_video():
    data = request.json
    dl_id = data.get('id', '')
    file_path = data.get('path', '')
    
    db = load_db()
    item = db.get(dl_id, {})
    real_path = resolve_video_file(file_path, item.get('save_path'), item.get('title'))
    
    if real_path and os.path.exists(real_path):
        os.startfile(real_path)
        if dl_id and dl_id in db and db[dl_id].get('filename') != real_path:
            db[dl_id]['filename'] = real_path
            save_db(db)
        return jsonify({'status': 'ok', 'resolved': real_path})
    
    folder = item.get('save_path') or (os.path.dirname(file_path) if file_path else '')
    if folder and os.path.exists(folder):
        os.startfile(folder)
        return jsonify({'status': 'ok', 'folder': folder})
    
    return jsonify({'error': 'File not found'}), 404


@app.route('/api/open-in-explorer', methods=['POST'])
def open_in_explorer():
    """Open Windows Explorer with the file selected."""
    data = request.json
    dl_id = data.get('id', '')
    file_path = data.get('path', '').strip()
    
    db = load_db()
    item = db.get(dl_id, {})
    real_path = resolve_video_file(file_path, item.get('save_path'), item.get('title'))

    if real_path and os.path.exists(real_path):
        subprocess.Popen(['explorer.exe', f'/select,{os.path.normpath(real_path)}'])
        if dl_id and dl_id in db and db[dl_id].get('filename') != real_path:
            db[dl_id]['filename'] = real_path
            save_db(db)
        return jsonify({'status': 'ok', 'resolved': real_path})

    folder = item.get('save_path') or (os.path.dirname(file_path) if file_path else '')
    if folder and os.path.exists(folder):
        subprocess.Popen(['explorer.exe', os.path.normpath(folder)])
        return jsonify({'status': 'ok', 'folder': folder})

    return jsonify({'error': 'Path not found'}), 404


@app.route('/api/pick-folder', methods=['POST'])
def pick_folder():
    """Open a native folder picker dialog and return the chosen path."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        folder = filedialog.askdirectory(title='اختر مجلد الحفظ')
        root.destroy()
        if folder:
            return jsonify({'path': folder.replace('/', '\\')})
        return jsonify({'path': None})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/stream-video')
def stream_video():
    """Stream a local video file to the browser player."""
    dl_id = request.args.get('id', '')
    path = request.args.get('path', '')
    
    db = load_db()
    item = db.get(dl_id, {})
    real_path = resolve_video_file(path, item.get('save_path'), item.get('title'))
    
    if real_path and os.path.isfile(real_path):
        if dl_id and dl_id in db and db[dl_id].get('filename') != real_path:
            db[dl_id]['filename'] = real_path
            save_db(db)
        return send_file(real_path, conditional=True)
    return jsonify({'error': 'Video file not found on disk'}), 404


def get_ydl_opts(quality, save_path, sid, dl_id):
    output_template = os.path.join(save_path, '%(title)s.%(ext)s') if save_path else '%(title)s.%(ext)s'

    format_selector = 'bestvideo+bestaudio/best'
    if quality == '1080p':
        format_selector = 'bestvideo[height<=1080]+bestaudio/best[height<=1080]'
    elif quality == '720p':
        format_selector = 'bestvideo[height<=720]+bestaudio/best[height<=720]'
    elif quality == '480p':
        format_selector = 'bestvideo[height<=480]+bestaudio/best[height<=480]'
    elif quality == 'mp3':
        format_selector = 'bestaudio/best'

    def post_hook(d):
        if d.get('status') == 'finished':
            f = d.get('info_dict', {}).get('filepath') or d.get('filepath')
            if f and os.path.isfile(f):
                db = load_db()
                if dl_id in db:
                    db[dl_id]['filename'] = f
                    save_db(db)

    ydl_opts = {
        'format': format_selector,
        'outtmpl': output_template,
        'logger': MyLogger(sid, dl_id),
        'progress_hooks': [lambda d: progress_hook(d, sid, dl_id)],
        'postprocessor_hooks': [post_hook],
        'ignoreerrors': True,
        'quiet': False,
        'merge_output_format': 'mp4',
    }

    ffmpeg_path = os.path.join(os.path.dirname(__file__), 'ffmpeg.exe')
    if os.path.exists(ffmpeg_path):
        ydl_opts['ffmpeg_location'] = ffmpeg_path

    if quality == 'mp3':
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    return ydl_opts


def start_download_thread(dl_id, url, quality, save_path, sid):
    active_downloads[dl_id] = {'pause': False, 'cancel': False}
    db = load_db()
    db[dl_id]['status'] = 'downloading'
    save_db(db)

    socketio.emit('status_change', {'id': dl_id, 'status': 'downloading'}, to=sid)

    ydl_opts = get_ydl_opts(quality, save_path, sid, dl_id)

    def download_runner():
        try:
            socketio.emit('log', {'msg': f"بدء التحميل: {db[dl_id].get('title','')}", 'id': dl_id}, to=sid)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info_dict = ydl.extract_info(url, download=True)

            if active_downloads.get(dl_id, {}).get('pause'):
                raise DownloadInterrupted("Paused")
            if active_downloads.get(dl_id, {}).get('cancel'):
                raise DownloadInterrupted("Cancelled")

            db2 = load_db()
            db2[dl_id]['status'] = 'completed'
            db2[dl_id]['percent'] = '100%'
            
            # Resolve the final saved file path
            final_file = None
            if info_dict:
                req = info_dict.get('requested_downloads')
                if req and len(req) > 0 and 'filepath' in req[0]:
                    final_file = req[0]['filepath']
                elif '_filename' in info_dict:
                    final_file = info_dict['_filename']

            resolved = resolve_video_file(final_file or db2[dl_id].get('filename'), save_path, db2[dl_id].get('title'))
            if resolved:
                db2[dl_id]['filename'] = resolved

            save_db(db2)
            socketio.emit('status_change', {'id': dl_id, 'status': 'completed', 'percent': '100%', 'filename': db2[dl_id].get('filename')}, to=sid)
            socketio.emit('log', {'msg': 'تم الانتهاء من التحميل بنجاح ✅', 'id': dl_id}, to=sid)

        except DownloadInterrupted as e:
            db2 = load_db()
            reason = str(e)
            new_status = 'paused' if reason == "Paused" else 'canceled'
            db2[dl_id]['status'] = new_status
            save_db(db2)
            socketio.emit('status_change', {'id': dl_id, 'status': new_status}, to=sid)
            socketio.emit('log', {'msg': f"{'تم إيقاف التحميل مؤقتاً ⏸' if new_status=='paused' else 'تم إلغاء التحميل ❌'}", 'id': dl_id}, to=sid)

        except Exception as e:
            db2 = load_db()
            db2[dl_id]['status'] = 'failed'
            db2[dl_id]['error'] = str(e)
            save_db(db2)
            socketio.emit('log', {'msg': f"خطأ: {str(e)}", 'id': dl_id}, to=sid)
            socketio.emit('status_change', {'id': dl_id, 'status': 'failed'}, to=sid)

        finally:
            if dl_id in active_downloads:
                del active_downloads[dl_id]

    thread = threading.Thread(target=download_runner)
    thread.daemon = True
    thread.start()


@socketio.on('start_download')
def handle_start_download(data):
    url = data.get('url')
    quality = data.get('quality')
    save_path = data.get('save_path', '')
    title = data.get('title', 'جاري الجلب...')
    thumbnail = data.get('thumbnail', '')
    sid = request.sid

    if save_path and not os.path.exists(save_path):
        try:
            os.makedirs(save_path, exist_ok=True)
        except:
            socketio.emit('log', {'msg': f"خطأ: لا يمكن إنشاء المجلد {save_path}"}, to=sid)
            return

    dl_id = str(uuid.uuid4())
    db = load_db()
    db[dl_id] = {
        'id': dl_id,
        'url': url,
        'quality': quality,
        'save_path': save_path,
        'title': title,
        'thumbnail': thumbnail,
        'status': 'starting',
        'percent': '0%',
        'filename': ''
    }
    save_db(db)

    socketio.emit('new_download', db[dl_id], to=sid)
    start_download_thread(dl_id, url, quality, save_path, sid)


@app.route('/api/pause', methods=['POST'])
def pause_download():
    dl_id = request.json.get('id')
    if dl_id in active_downloads:
        active_downloads[dl_id]['pause'] = True
        return jsonify({'status': 'pausing'})
    return jsonify({'error': 'Not active'}), 400


@app.route('/api/cancel', methods=['POST'])
def cancel_download():
    dl_id = request.json.get('id')
    if dl_id in active_downloads:
        active_downloads[dl_id]['cancel'] = True
    db = load_db()
    if dl_id in db:
        db[dl_id]['status'] = 'canceled'
        save_db(db)
    return jsonify({'status': 'canceling'})


@app.route('/api/resume', methods=['POST'])
def resume_download():
    data = request.json
    dl_id = data.get('id')
    sid = data.get('sid', '')
    db = load_db()
    if dl_id in db and db[dl_id]['status'] in ['paused', 'failed', 'canceled']:
        start_download_thread(dl_id, db[dl_id]['url'], db[dl_id]['quality'], db[dl_id]['save_path'], sid)
        return jsonify({'status': 'resuming'})
    return jsonify({'error': 'Cannot resume'}), 400


if __name__ == '__main__':
    socketio.run(app, debug=True, allow_unsafe_werkzeug=True)
