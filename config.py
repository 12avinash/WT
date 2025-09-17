import os
from datetime import timedelta

class Config:
    # Default secret key for local/dev use
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key')
    UPLOAD_FOLDER = 'uploads'
    TEMP_FOLDER = 'temp'
    SESSION_TIMEOUT = timedelta(hours=24)
    SUPPORTED_FORMATS = ['mp4', 'mkv', 'mov', 'avi']
    CHUNK_SIZE = 8192

class ProductionConfig(Config):
    # In production, must set SECRET_KEY explicitly
    SECRET_KEY = os.environ.get('SECRET_KEY')
    if not SECRET_KEY:
        # just log a warning instead of crashing (optional)
        print("[WARNING] No SECRET_KEY set for production — using insecure fallback")
        SECRET_KEY = 'change-me-in-prod'