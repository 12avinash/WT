import os
import uuid
import time
import mimetypes
import threading
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, Response, send_from_directory
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.utils import secure_filename
from config import Config

app = Flask(__name__, static_folder='.', static_url_path='/')
app.config.from_object(Config)

# SocketIO with proper configuration
socketio = SocketIO(
    app, 
    cors_allowed_origins="*", 
    max_http_buffer_size=50*1024*1024,  # 50MB for large video uploads
    ping_timeout=60,
    ping_interval=25
)

# Ensure folders exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['TEMP_FOLDER'], exist_ok=True)

# In-memory session storage (swap to Redis/DB for production)
active_sessions = {}   # session_code -> WatchSession
user_sessions = {}     # socket_id -> session_code


class WatchSession:
    def __init__(self, session_id, host_socket_id=None):
        self.session_id = session_id
        self.host_socket_id = host_socket_id
        self.video_path = None
        self.video_filename = None
        self.current_time = 0.0
        self.is_playing = False
        self.created_at = datetime.now()
        self.participants = set()
        self.chat_messages = []
        self.last_update = time.time()

    def to_dict(self):
        return {
            'session_id': self.session_id,
            'host_socket_id': self.host_socket_id,
            'video_filename': self.video_filename,
            'current_time': self.current_time,
            'is_playing': self.is_playing,
            'participant_count': len(self.participants),
            'created_at': self.created_at.isoformat(),
            'chat_messages': self.chat_messages[-50:]  # Last 50 messages
        }

    def update_time(self):
        """Update current time based on playback state"""
        if self.is_playing:
            now = time.time()
            time_diff = now - self.last_update
            self.current_time += time_diff
        self.last_update = time.time()


def generate_session_code():
    """Generate a unique 6-character session code"""
    import random
    import string
    while True:
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if code not in active_sessions:
            return code


def cleanup_old_sessions():
    """Background thread to clean up expired sessions"""
    while True:
        try:
            cutoff = datetime.now() - app.config['SESSION_TIMEOUT']
            to_delete = []
            
            for code, session in list(active_sessions.items()):
                if session.created_at < cutoff or len(session.participants) == 0:
                    to_delete.append(code)
                    # Clean up video file
                    if session.video_path and os.path.exists(session.video_path):
                        try:
                            os.remove(session.video_path)
                            print(f"Cleaned up video file for session {code}")
                        except Exception as e:
                            print(f"Error cleaning up video file: {e}")
            
            for code in to_delete:
                if code in active_sessions:
                    del active_sessions[code]
                    print(f"Removed expired session: {code}")
                    
        except Exception as e:
            print(f"Error in cleanup thread: {e}")
        
        time.sleep(300)  # Check every 5 minutes


# Start cleanup thread
threading.Thread(target=cleanup_old_sessions, daemon=True).start()


# Routes
@app.route('/')
def index():
    """Serve the main HTML page"""
    return send_from_directory('.', 'join.html')


@app.route('/api/upload', methods=['POST'])
def upload_video():
    """Handle video upload and session creation"""
    try:
        if 'video' not in request.files:
            return jsonify({'error': 'No video file provided'}), 400
        
        file = request.files['video']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400

        # Validate file size (limit to 2GB)
        if hasattr(file, 'content_length') and file.content_length > 2 * 1024 * 1024 * 1024:
            return jsonify({'error': 'File too large (max 2GB)'}), 400

        # Validate extension
        filename = secure_filename(file.filename)
        if not filename:
            return jsonify({'error': 'Invalid filename'}), 400
            
        ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
        if ext not in app.config['SUPPORTED_FORMATS']:
            return jsonify({'error': f'Unsupported format. Supported: {", ".join(app.config["SUPPORTED_FORMATS"])}'}), 400

        # Generate session and save file
        session_code = generate_session_code()
        safe_name = f"{session_code}_{int(time.time())}_{filename}"
        dest_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_name)
        
        # Save file
        file.save(dest_path)
        
        # Create session
        session = WatchSession(session_code)
        session.video_path = dest_path
        session.video_filename = filename
        active_sessions[session_code] = session

        print(f"Created session {session_code} with video {filename}")
        
        return jsonify({
            'session_code': session_code,
            'filename': filename,
            'message': 'Video uploaded and session created successfully'
        })
        
    except Exception as e:
        print(f"Upload error: {e}")
        return jsonify({'error': 'Upload failed'}), 500


@app.route('/api/session/<session_code>')
def get_session(session_code):
    """Get session information"""
    code = session_code.upper()
    if code not in active_sessions:
        return jsonify({'error': 'Session not found'}), 404
    
    session = active_sessions[code]
    session.update_time()  # Update current time
    return jsonify(session.to_dict())


@app.route('/api/video/<session_code>')
def stream_video(session_code):
    """Stream video with byte-range support for smooth seeking"""
    code = session_code.upper()
    if code not in active_sessions:
        return jsonify({'error': 'Session not found'}), 404
    
    session = active_sessions[code]
    if not session.video_path or not os.path.exists(session.video_path):
        return jsonify({'error': 'Video file not found'}), 404

    file_path = session.video_path
    file_size = os.path.getsize(file_path)
    
    # Determine MIME type
    mime_type, _ = mimetypes.guess_type(session.video_filename)
    if mime_type is None:
        mime_type = 'video/mp4'  # Default fallback

    def generate_stream(start, length):
        with open(file_path, 'rb') as f:
            f.seek(start)
            remaining = length
            chunk_size = app.config.get('CHUNK_SIZE', 8192)
            while remaining > 0:
                read_size = min(chunk_size, remaining)
                chunk = f.read(read_size)
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    # Handle range requests for video seeking
    range_header = request.headers.get('Range')
    if range_header:
        try:
            # Parse range header: "bytes=start-end"
            byte_start = 0
            byte_end = file_size - 1
            
            range_match = range_header.replace('bytes=', '')
            if '-' in range_match:
                start_str, end_str = range_match.split('-', 1)
                if start_str:
                    byte_start = int(start_str)
                if end_str:
                    byte_end = int(end_str)
            
            # Validate range
            if byte_start >= file_size or byte_start < 0:
                return Response(status=416)  # Range Not Satisfiable
            
            byte_end = min(byte_end, file_size - 1)
            content_length = byte_end - byte_start + 1
            
            # Create partial content response
            response = Response(
                generate_stream(byte_start, content_length),
                206,  # Partial Content
                mimetype=mime_type,
                direct_passthrough=True
            )
            response.headers.add('Content-Range', f'bytes {byte_start}-{byte_end}/{file_size}')
            response.headers.add('Accept-Ranges', 'bytes')
            response.headers.add('Content-Length', str(content_length))
            response.headers.add('Cache-Control', 'no-cache')
            
            return response
            
        except (ValueError, IndexError):
            # Invalid range header, return full content
            pass
    
    # Return full content
    response = Response(
        generate_stream(0, file_size),
        mimetype=mime_type,
        direct_passthrough=True
    )
    response.headers.add('Content-Length', str(file_size))
    response.headers.add('Accept-Ranges', 'bytes')
    response.headers.add('Cache-Control', 'no-cache')
    
    return response


# Socket.IO Events
@socketio.on('connect')
def on_connect():
    """Handle client connection"""
    print(f"Client connected: {request.sid}")
    emit('connected', {'socket_id': request.sid})


@socketio.on('disconnect')
def on_disconnect():
    """Handle client disconnection"""
    sid = request.sid
    print(f"Client disconnected: {sid}")
    
    if sid in user_sessions:
        session_code = user_sessions[sid]
        if session_code in active_sessions:
            session = active_sessions[session_code]
            session.participants.discard(sid)
            
            # Notify other participants
            emit('user_left', {
                'socket_id': sid,
                'participant_count': len(session.participants)
            }, room=session_code)
            
            # Handle host disconnection
            if session.host_socket_id == sid:
                session.host_socket_id = None
                emit('host_disconnected', {
                    'message': 'Host disconnected'
                }, room=session_code)
        
        del user_sessions[sid]


@socketio.on('join_session')
def on_join_session(data):
    """Handle user joining a session"""
    try:
        session_code = (data.get('session_code') or '').upper().strip()
        is_host = bool(data.get('is_host', False))
        username = data.get('username', f'User{request.sid[:6]}')
        
        if not session_code or len(session_code) != 6:
            emit('error', {'message': 'Invalid session code'})
            return
        
        if session_code not in active_sessions:
            emit('error', {'message': 'Session not found'})
            return
        
        session = active_sessions[session_code]
        
        # Set host if this is host joining
        if is_host and not session.host_socket_id:
            session.host_socket_id = request.sid
        
        # Add user to session
        session.participants.add(request.sid)
        user_sessions[request.sid] = session_code
        join_room(session_code)
        
        # Update session time
        session.update_time()
        
        print(f"User {username} joined session {session_code} (host: {is_host})")
        
        # Send session info to the joining user
        emit('session_joined', {
            'session_code': session_code,
            'is_host': request.sid == session.host_socket_id,
            'video_filename': session.video_filename,
            'current_time': session.current_time,
            'is_playing': session.is_playing,
            'participant_count': len(session.participants),
            'chat_messages': session.chat_messages[-50:]
        })
        
        # Notify other participants
        emit('user_joined', {
            'socket_id': request.sid,
            'username': username,
            'participant_count': len(session.participants),
            'is_host': request.sid == session.host_socket_id
        }, room=session_code, include_self=False)
        
    except Exception as e:
        print(f"Error joining session: {e}")
        emit('error', {'message': 'Failed to join session'})


@socketio.on('video_action')
def on_video_action(data):
    """Handle video playback actions from host"""
    try:
        sid = request.sid
        if sid not in user_sessions:
            emit('error', {'message': 'Not in a session'})
            return
        
        session_code = user_sessions[sid]
        session = active_sessions[session_code]
        
        # Only host can control video
        if sid != session.host_socket_id:
            emit('error', {'message': 'Only the host can control video playback'})
            return
        
        action = data.get('action')
        current_time = float(data.get('current_time', session.current_time))
        
        # Update session state
        session.update_time()
        session.current_time = current_time
        
        if action == 'play':
            session.is_playing = True
        elif action == 'pause':
            session.is_playing = False
        elif action == 'seek':
            # Seeking doesn't change play state, just position
            pass
        
        session.last_update = time.time()
        
        print(f"Video action in {session_code}: {action} at {current_time:.2f}s")
        
        # Broadcast to all participants
        emit('video_sync', {
            'action': action,
            'current_time': session.current_time,
            'is_playing': session.is_playing,
            'timestamp': time.time()
        }, room=session_code)
        
    except Exception as e:
        print(f"Error handling video action: {e}")
        emit('error', {'message': 'Video action failed'})


@socketio.on('chat_message')
def on_chat_message(data):
    """Handle chat messages"""
    try:
        sid = request.sid
        if sid not in user_sessions:
            emit('error', {'message': 'Not in a session'})
            return
        
        session_code = user_sessions[sid]
        session = active_sessions[session_code]
        
        username = data.get('username', f'User{sid[:6]}')
        message_text = data.get('message', '').strip()
        
        if not message_text:
            return
        
        # Create message object
        message = {
            'id': str(uuid.uuid4()),
            'user_id': sid,
            'username': username,
            'message': message_text,
            'timestamp': datetime.now().isoformat(),
            'is_host': sid == session.host_socket_id
        }
        
        # Add to session chat history
        session.chat_messages.append(message)
        
        # Keep only last 200 messages
        if len(session.chat_messages) > 200:
            session.chat_messages = session.chat_messages[-200:]
        
        print(f"Chat message in {session_code} from {username}: {message_text}")
        
        # Broadcast to all participants
        emit('new_message', message, room=session_code)
        
    except Exception as e:
        print(f"Error handling chat message: {e}")
        emit('error', {'message': 'Failed to send message'})


@socketio.on('ping')
def on_ping():
    """Handle ping for connection testing"""
    emit('pong', {'timestamp': time.time()})


@socketio.on_error_default
def default_error_handler(e):
    """Handle socket errors"""
    print(f"SocketIO error: {e}")
    emit('error', {'message': 'An error occurred'})


# Error handlers
@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not found'}), 404


@app.errorhandler(500)
def internal_error(error):
    return jsonify({'error': 'Internal server error'}), 500


if __name__ == '__main__':
    print("Starting Movie Watch Party server...")
    print(f"Upload folder: {app.config['UPLOAD_FOLDER']}")
    print(f"Supported formats: {app.config['SUPPORTED_FORMATS']}")
    print("Server running on http://0.0.0.0:5000/")
    
    socketio.run(
        app, 
        host='0.0.0.0', 
        port=5000, 
        debug=True,
        allow_unsafe_werkzeug=True  # For development only
    )