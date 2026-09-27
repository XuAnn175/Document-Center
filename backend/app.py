# backend/app.py
from __future__ import annotations
import os, time
from datetime import datetime

from flask import Flask, request, jsonify, send_from_directory, current_app, redirect, url_for, render_template
from flask_cors import CORS
from flask_login import (
    LoginManager, login_user, logout_user,
    login_required, current_user,
)
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, make_transient_to_detached
from dotenv import load_dotenv
from authlib.integrations.flask_client import OAuth
from prometheus_flask_exporter import PrometheusMetrics

from .models import (
    db, File, Folder, User, FileVersion,
    DocumentReview, Notification,
    generate_reset_token, verify_reset_token,
)
from .config import Config
from .init_db import create_test_user, create_admin_and_test_users
from .redis_client import (
    PUBLIC_FILES_KEY, PUBLIC_FILES_TTL, USER_TTL, user_key,
    cache_get_json, cache_set_json,
    hit_rate_limit, reset_rate_limit,
    register_cache_invalidation,
)


# ───────────────────────────── Flask & Login ─────────────────────────────
app = Flask(__name__)
app.config.from_object(Config)
db.init_app(app)

# Behind nginx (and, in k8s, an ingress controller) request.remote_addr is the
# proxy's address. Rate limits key on the client IP, so recover it from
# X-Forwarded-For, trusting only as many hops as there are proxies.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=Config.TRUSTED_PROXY_COUNT)

register_cache_invalidation(File, User)

metrics = PrometheusMetrics(app)  # 啟用自動監控所有 endpoint

login_manager               = LoginManager()
login_manager.login_view    = None          #   ↳ return JSON, no 302
login_manager.init_app(app)

# create test user and admin 
create_admin_and_test_users(app, db)

# google oauth
load_dotenv()
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "your-google-client-id")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "your-google-client-secret")
FRONTEND_PORT = os.getenv("FRONTEND_PORT", 80)
FRONTEND_ROOT = f"http://localhost:{FRONTEND_PORT}" if FRONTEND_PORT else "http://localhost:8080"

oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    # access_token_url='https://accounts.google.com/o/oauth2/token',
    access_token_params=None,
    # authorize_url='https://accounts.google.com/o/oauth2/auth',
    authorize_params=None,
    api_base_url='https://www.googleapis.com/oauth2/v1/',
    client_kwargs={
        'scope': 'openid email profile',
    },
    server_metadata_url= 'https://accounts.google.com/.well-known/openid-configuration'
)

def _user_to_cache(user: User) -> dict:
    # password_hash is deliberately left out so hashes never sit in Redis.
    # The few paths that need it (change-password) load it on demand.
    return {
        "username":   user.username,
        "email":      user.email,
        "grade":      user.grade,
        "is_admin":   bool(user.is_admin),
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }

def _user_from_cache(uid: int, data: dict) -> User:
    user = User(
        id=uid,
        username=data["username"],
        email=data["email"],
        grade=data["grade"],
        is_admin=data["is_admin"],
        created_at=datetime.fromisoformat(data["created_at"]) if data["created_at"] else None,
    )
    # Attach to the session without querying. A plain detached object would
    # look fine but silently drop writes: current_user.set_password() followed
    # by commit() would never reach the database. Attributes not cached here
    # (password_hash) are marked expired and loaded on first access.
    make_transient_to_detached(user)
    return db.session.merge(user, load=False)

@login_manager.user_loader
def load_user(uid):
    # Runs on every authenticated request, and the SPA polls /session-status
    # every 10 s and /notifications every 30 s per open tab, so without a cache
    # this is a user-table read several times a minute per idle tab.
    uid = int(uid)
    cached = cache_get_json(user_key(uid))
    if cached is not None:
        return _user_from_cache(uid, cached)
    user = db.session.get(User, uid)
    if user is not None:
        cache_set_json(user_key(uid), _user_to_cache(user), USER_TTL)
    return user

@login_manager.unauthorized_handler
def _unauth():
    return jsonify({"error": "unauthenticated"}), 401

CORS(app, supports_credentials=True)

# ───────────────────── helper: build **absolute** disk path ──────────────
def folder_disk_path(folder: Folder | None, username: str) -> str:
    """
    Build uploads/<username>/… path for *folder*.
    If *folder* is None ⇒ returns user root folder.
    """
    if not folder or folder.parent_id is None:
        return os.path.join(app.config['UPLOAD_FOLDER'], username)
    
    # Build the full path hierarchy recursively
    path_parts = []
    current = folder
    
    while current and current.parent_id is not None:
        path_parts.append(current.name)
        current = Folder.query.get(current.parent_id)
    
    path_parts.reverse()  # Reverse to get correct order
    return os.path.join(app.config['UPLOAD_FOLDER'], username, *path_parts)

def get_version_dir(username: str) -> str:
    """Returns the .version directory for a user"""
    return os.path.join(Config.UPLOAD_FOLDER, username, '.version')

def get_next_version_number(file_id: int) -> int:
    """Get the next available version number for a file"""
    highest_version = db.session.query(db.func.max(FileVersion.version_number)).filter_by(file_id=file_id).scalar()
    return (highest_version or 0) + 1

# Helper function to get content of a specific version
def _get_content_for_version(db_session: Session, file_obj: File, version_number_to_fetch: int | None) -> str:
    """Fetches the content for a given file and version number.
    Assumes FileVersion(N).path stores the actual content of version N.
    """
    if version_number_to_fetch is None:
        current_app.logger.info(f"Requested version is None for file {file_obj.id}. Returning empty content.")
        return ""

    # Ensure file_obj is associated with the current session
    if file_obj not in db_session:
        file_obj = db_session.merge(file_obj)

    fv_record = db_session.query(FileVersion).filter_by(file_id=file_obj.id, version_number=version_number_to_fetch).first()

    if not fv_record:
        current_app.logger.warning(f"FileVersion record not found for file {file_obj.id}, version {version_number_to_fetch}.")
        return ""
        
    content_path = fv_record.path
    
    if content_path and os.path.exists(content_path):
        try:
            with open(content_path, 'r', encoding='utf-8') as f:
                content = f.read()
            current_app.logger.info(f"Successfully read content for file {file_obj.id} V{version_number_to_fetch} from {content_path}. Length: {len(content)}")
            return content
        except Exception as e:
            current_app.logger.error(f"Error reading content for file {file_obj.id} V{version_number_to_fetch} from {content_path}: {e}", exc_info=True)
            return ""
    else:
        current_app.logger.warning(f"Content path for file {file_obj.id} V{version_number_to_fetch} not found or does not exist. Path: {content_path}")
        return ""

# ─────────────────────────── rate limits ───────────────────────────
# (max attempts, window in seconds)
LOGIN_LIMIT_PER_IP       = (20, 60)
LOGIN_LIMIT_PER_USERNAME = (5, 300)
REGISTER_LIMIT_PER_IP    = (10, 3600)
RESET_LIMIT_PER_IP       = (5, 900)
RESET_LIMIT_PER_EMAIL    = (3, 3600)

def _too_many_attempts(retry_after: int):
    resp = jsonify({"error": "Too many attempts. Please try again later."})
    resp.status_code = 429
    resp.headers["Retry-After"] = str(retry_after)
    return resp

# ───────────────────────────────── Routes ─────────────────────────────────
@app.route('/')
def health():
    return {"message": "Flask backend is running."}

# ────────────── Upload ───────────────────────────────────────────────────
@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    f         = request.files.get('file')
    folder_id = request.form.get('folder_id', type=int)
    if not f:
        return {"error": "No file provided"}, 400

    # target folder (None → root)
    folder = Folder.query.get(folder_id) if folder_id else \
             Folder.query.filter_by(owner_id=current_user.id,
                                    parent_id=None).first()

    disk_dir = folder_disk_path(folder, current_user.username)
    os.makedirs(disk_dir, exist_ok=True)

    final_path = os.path.join(disk_dir, secure_filename(f.filename))
    f.save(final_path)

    # Create file record
    rec = File(filename=f.filename, mimetype=f.mimetype,
               path=final_path, owner_id=current_user.id,
               folder_id=folder.id if folder else None,
               current_version=1)  # Set initial version
    db.session.add(rec)
    db.session.flush()  # Get the file ID without committing

    # Create initial version record and store in .version directory
    version_dir = get_version_dir(current_user.username)
    os.makedirs(version_dir, exist_ok=True)
    version_path = os.path.join(version_dir, f"{rec.id}_v1_{secure_filename(f.filename)}")
    import shutil
    shutil.copy2(final_path, version_path)

    version = FileVersion(
        file_id=rec.id,
        version_number=1,
        path=version_path,
        comment="Initial version"
    )
    db.session.add(version)
    db.session.commit()
    
    return {"message": "Upload successful", "file_id": rec.id}, 201

# ────────────── Download / Delete file ───────────────────────────────────
@app.route('/download/<int:file_id>')
@login_required
def download_file(file_id):
    rec = File.query.get_or_404(file_id)
    if rec.is_published == False and rec.owner_id != current_user.id and not current_user.is_admin:
        return {"error": "Access denied"}, 403
    return send_from_directory(os.path.dirname(rec.path),
                               os.path.basename(rec.path))

@app.route('/delete/<int:file_id>', methods=['DELETE'])
@login_required
def delete_file(file_id):
    rec = File.query.get_or_404(file_id)
    if rec.owner_id != current_user.id and not current_user.is_admin:
        return {"error": "Access denied"}, 403

    try:
        # Delete the file record - this will cascade delete all related versions
        # due to the cascade relationship we just added
        db.session.delete(rec)
        db.session.commit()
        
        return {
            "message": "File deleted successfully.",
            "file_id": file_id
        }, 200
        
    except Exception as e:
        db.session.rollback()
        print(f"Error deleting file {file_id}: {str(e)}")
        return {"error": "Failed to delete file"}, 500

@app.route('/list-deleted-files', methods=['GET'])
@login_required
def list_deleted_files():
    """
    List all files that have been deleted but still have version history.
    This helps users find files they might want to restore.
    """
    # Find all version records that don't have an associated file
    versions = db.session.query(FileVersion).outerjoin(File).filter(File.id == None).all()
    
    # Filter versions to only include those belonging to current user
    user_username = current_user.username
    user_versions = [v for v in versions if f"/{user_username}/" in v.path or v.path.startswith(f"{user_username}/")]
    
    # Group versions by file_id to get the latest version for each deleted file
    deleted_files = {}
    for version in user_versions:
        if version.file_id not in deleted_files or version.version_number > deleted_files[version.file_id]['version_number']:
            deleted_files[version.file_id] = {
                'file_id': version.file_id,
                'version_number': version.version_number,
                'filename': os.path.basename(version.path).split('_', 1)[1],  # Remove version prefix
                'last_modified': version.uploaded_at.isoformat(),
                'size': os.path.getsize(version.path) if os.path.exists(version.path) else 0,
                'comment': version.comment
            }
    
    return jsonify(list(deleted_files.values()))

@app.route('/permanently-delete/<int:file_id>', methods=['DELETE'])
@login_required
def permanently_delete_file(file_id):
    """
    Permanently delete a file and all its versions.
    This is a destructive operation and cannot be undone.
    """
    # First check if the file exists
    file = File.query.get(file_id)
    if file:
        if file.owner_id != current_user.id and not current_user.is_admin:
            return {"error": "Access denied"}, 403
        db.session.delete(file)
    
    # Delete all versions
    versions = FileVersion.query.filter_by(file_id=file_id).all()
    for version in versions:
        try:
            if os.path.exists(version.path):
                os.remove(version.path)
        except FileNotFoundError:
            pass
        db.session.delete(version)
    
    db.session.commit()
    return {
        "message": "File and all its versions permanently deleted",
        "file_id": file_id
    }, 200

# ────────────── Folder tree (GET) ────────────────────────────────────────
@app.route('/folders', methods=['GET'])
@login_required
def get_folders():
    def ser(folder: Folder):
        return {
            "id": folder.id, "name": folder.name,
            "parent_id": folder.parent_id,
            "files": [
                {
                    "id": f.id, "name": f.filename,
                    "mimetype": f.mimetype,
                    "uploaded_at": f.uploaded_at.isoformat(),
                    "is_under_review": f.is_under_review,
                    "is_published": f.is_published,
                    "active_review": {
                        "id": f.get_active_review().id,
                        "reviewer": f.get_active_review().reviewer.username,
                        "requested_at": f.get_active_review().requested_at.isoformat()
                    } if f.get_active_review() else None
                } for f in folder.files if f.owner_id == current_user.id
            ],
            "children": [ser(ch) for ch in folder.subfolders if ch.owner_id == current_user.id]
        }
    
    def get_flat_folders(folder, depth=0):
        """Get a flat list of folders (excluding root) for dropdown"""
        folders = []
        if folder:
            for child in folder.subfolders:
                indent = '  ' * depth
                folders.append({
                    'id': child.id,
                    'name': f"{indent}{child.name}"
                })
                folders.extend(get_flat_folders(child, depth + 1))
        return folders
    
    root = Folder.query.filter_by(owner_id=current_user.id,
                                  parent_id=None).first()
    
    folder_tree = ser(root) if root else {"id": None, "name": "", "files": [], "children": []}
    flat_folders = get_flat_folders(root)
    
    return jsonify({
        "tree": folder_tree,
        "flat": flat_folders
    })

# ────────────── Get Public Files ────────────────────────────────────────
def _public_files_payload() -> dict:
    all_publics = File.query.filter_by(is_published=True).all()

    files_data = []
    for file in all_publics:
        files_data.append({
            "id": file.id,
            "name": file.filename,
            "mimetype": file.mimetype,
            "path": file.path,
            "uploaded_at": file.uploaded_at.isoformat(),
            "is_under_review": file.is_under_review,
            "is_published": file.is_published,
            "current_version": file.current_version,
            # "active_review": {
            #     "id": file.get_active_review().id if file.get_active_review() else None,
            #     "reviewer": file.get_active_review().reviewer.username if file.get_active_review() else None,
            #     "requested_at": file.get_active_review().requested_at.isoformat() if file.get_active_review() else None
            # }
        })

    return {"pfiles": files_data}

@app.route('/public-files', methods=['GET'])
@login_required
def get_all_public_files():
    # Every user sees the same list, and it changes only when a file is
    # published, unpublished, edited, renamed, moved or deleted, so one shared
    # entry serves the whole site. Invalidation is automatic: any committed
    # change to a File row evicts it (see register_cache_invalidation).
    payload = cache_get_json(PUBLIC_FILES_KEY)
    if payload is None:
        payload = _public_files_payload()
        cache_set_json(PUBLIC_FILES_KEY, payload, PUBLIC_FILES_TTL)
    return jsonify(payload)

# ────────────── Create / Delete folder ───────────────────────────────────
@app.route('/folders', methods=['POST'])
@login_required
def create_folder():
    data      = request.json or {}
    name      = data.get('name', '').strip()
    parent_id = data.get('parent_id')
    if not name:
        return {"error": "Folder name required"}, 400

    parent = Folder.query.get(parent_id) if parent_id else \
             Folder.query.filter_by(owner_id=current_user.id,
                                    parent_id=None).first()

    # no duplicates among siblings
    if Folder.query.filter_by(owner_id=current_user.id,
                              parent_id=parent.id if parent else None,
                              name=name).first():
        return {"error": "Folder already exists"}, 400

    # Create folder in the correct parent directory
    parent_disk_path = folder_disk_path(parent, current_user.username)
    disk_dir = os.path.join(parent_disk_path, name)
    os.makedirs(disk_dir, exist_ok=True)

    new = Folder(name=name, owner_id=current_user.id,
                 parent_id=parent.id if parent else None)
    db.session.add(new)
    db.session.commit()
    return {"message": "Folder created", "folder_id": new.id}, 201

@app.route('/folders/<int:fid>', methods=['DELETE'])
@login_required
def delete_folder(fid):
    fld = Folder.query.get_or_404(fid)
    if fld.owner_id != current_user.id:
        return {"error": "Access denied"}, 403
    if fld.parent_id is None:
        return {"error": "Cannot delete root folder"}, 400
    db.session.delete(fld); db.session.commit()
    return {"message": "Folder deleted"}

# ────────────── Move file ────────────────────────────────────────────────
@app.route('/move-file', methods=['POST'])
@login_required
def move_file():
    """
    JSON body:
      { "file_id": 123, "target_folder_id": 7 }
    Note: target_folder_id can be null for moving to root folder
    """
    data         = request.json or {}
    file_id      = data.get('file_id')
    target_id    = data.get('target_folder_id')

    rec = File.query.get_or_404(file_id)
    
    # Handle moving to root folder (target_id is None/null)
    if target_id is None:
        # Get user's root folder
        target = Folder.query.filter_by(owner_id=current_user.id, parent_id=None).first()
        if not target:
            return {"error": "Root folder not found"}, 404
    else:
        target = Folder.query.get_or_404(target_id)
        if target.owner_id != current_user.id:
            return {"error": "Access denied to target folder"}, 403

    # ── ownership guards ──────────────────────────────────────
    if rec.owner_id != current_user.id:
        return {"error": "Access denied to file"}, 403

    # Check if file is already in the target folder
    if rec.folder_id == target.id:
        return {"error": "File is already in the target folder"}, 400

    # ── new disk location ------------------------------------
    dest_dir = folder_disk_path(target, current_user.username)
    os.makedirs(dest_dir, exist_ok=True)

    new_path = os.path.join(dest_dir, rec.filename)
    
    # Check if a file with the same name already exists in destination
    if os.path.exists(new_path) and new_path != rec.path:
        return {"error": f"A file named '{rec.filename}' already exists in the destination folder"}, 400
    
    try:
        # Use shutil.move instead of os.rename for better cross-device support
        # and handling of moves between different directory levels
        import shutil
        shutil.move(rec.path, new_path)
    except OSError as e:
        return {"error": f"Failed to move file: {str(e)}"}, 500
    except Exception as e:
        return {"error": f"Unexpected error while moving file: {str(e)}"}, 500

    # ── update DB --------------------------------------------
    rec.folder_id = target.id
    rec.path      = new_path
    db.session.commit()

    return {"message": "File moved successfully"}, 200

# ────────────── Text File Edit (New Endpoints) ───────────────────────────
@app.route('/file-content/<int:file_id>', methods=['GET'])
@login_required
def get_file_content(file_id):
    file = File.query.get_or_404(file_id)
    if file.owner_id != current_user.id: # Add admin check if necessary: and not current_user.is_admin
        return {"error": "Access denied to file"}, 403

    # Basic check for text files, can be expanded (e.g., check file.mimetype)
    if not file.filename.lower().endswith(('.txt', '.md', '.py', '.js', '.json', '.yaml', '.yml', '.html', '.css')):
        return {"error": "File is not a supported editable text file type"}, 400

    try:
        # Ensure the path stored in db is the actual current path
        # (especially if versioning updates file.path)
        actual_path = file.path 
        if not os.path.exists(actual_path):
             # Fallback to version path if main path deleted or points to a version in .version
            if file.current_version:
                latest_version = FileVersion.query.filter_by(file_id=file.id, version_number=file.current_version).first()
                if latest_version and os.path.exists(latest_version.path):
                    actual_path = latest_version.path
                else:
                    return {"error": "File content not found on disk"}, 404
            else: # No versions and main path missing
                return {"error": "File content not found on disk"}, 404


        with open(actual_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return jsonify({"content": content, "filename": file.filename, "mimetype": file.mimetype})
    except Exception as e:
        return {"error": f"Error reading file: {str(e)}"}, 500

@app.route('/file-content/<int:file_id>', methods=['POST'])
@login_required
def save_file_content(file_id):
    file = File.query.get_or_404(file_id)
    if file.owner_id != current_user.id:
        return {"error": "Access denied to file"}, 403
    if file.is_under_review:
        return {"error": "Cannot edit file while it is under review"}, 403
    if not file.filename.lower().endswith(('.txt', '.md', '.py', '.js', '.json', '.yaml', '.yml', '.html', '.css')):
        return {"error": "File is not a supported editable text file type"}, 400

    data = request.json
    new_content = data.get('content')
    if new_content is None:
        return {"error": "No content provided"}, 400

    try:
        username = User.query.get(file.owner_id).username
        version_dir = get_version_dir(username)
        os.makedirs(version_dir, exist_ok=True)
        
        next_version_number = get_next_version_number(file_id)
        
        base_name = os.path.splitext(file.filename)[0]
        ext = os.path.splitext(file.filename)[1]
        # Path for the new version's content file in the .version directory
        new_version_content_path = os.path.join(version_dir, f"{base_name}_v{next_version_number}{ext}")

        # 1. Save the new content to its dedicated version file
        with open(new_version_content_path, 'w', encoding='utf-8') as f_version:
            f_version.write(new_content)

        # 2. Create the FileVersion record pointing to this new version file
        version_record = FileVersion(
            file_id=file_id,
            version_number=next_version_number,
            path=new_version_content_path,
            comment=f"Version {next_version_number}"
        )
        db.session.add(version_record)
        
        # 3. Update the main file (File.path) to reflect the new content
        # This ensures File.path always has the content of File.current_version
        # The actual live file path might be different from version files if desired, 
        # but for simplicity here, we can copy from the version file.
        live_file_path = file.path
        os.makedirs(os.path.dirname(live_file_path), exist_ok=True)
        import shutil
        shutil.copy2(new_version_content_path, live_file_path)
        
        # 4. Update file record metadata
        file.current_version = next_version_number
        file.updated_at = datetime.utcnow()

        file.is_published = False  # Reset publish status on edit
        
        db.session.commit()
        
        current_app.logger.info(f"File {file_id} saved. New version {next_version_number} created at {new_version_content_path}. Live file {live_file_path} updated.")
        return {"message": "File saved successfully", "version": next_version_number}
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error saving file {file_id}: {str(e)}", exc_info=True)
        return {"error": f"Error saving file: {str(e)}"}, 500

# ────────────── Rename File ──────────────────────────────────────────────
@app.route('/rename-file/<int:file_id>', methods=['POST'])
@login_required
def rename_file(file_id):
    file_to_rename = File.query.get_or_404(file_id)
    if file_to_rename.owner_id != current_user.id: # Add admin check if necessary
        return {"error": "Access denied to file"}, 403

    data = request.json
    new_filename = data.get('new_filename')

    if not new_filename or not new_filename.strip():
        return {"error": "New filename cannot be empty"}, 400
    
    new_filename = secure_filename(new_filename.strip())
    if not new_filename: # secure_filename might return empty if the name is really bad
        return {"error": "Invalid new filename"}, 400

    if new_filename == file_to_rename.filename:
        return {"message": "Filename is the same, no changes made"}, 200 # Or a 400 if preferred

    # Determine the directory of the file
    current_dir = os.path.dirname(file_to_rename.path)
    new_path = os.path.join(current_dir, new_filename)

    if os.path.exists(new_path):
        return {"error": f"A file named '{new_filename}' already exists in this folder"}, 400

    try:
        # Rename on disk
        os.rename(file_to_rename.path, new_path)

        # Update database
        file_to_rename.filename = new_filename
        file_to_rename.path = new_path
        file_to_rename.updated_at = datetime.utcnow() # Update modified time
        db.session.commit()

        # Note: This does not rename files in the .version directory.
        # Version history will still point to old version paths with old names.
        # If versions also need renaming, that would be significantly more complex.

        return {"message": "File renamed successfully", "new_filename": new_filename, "new_path": new_path}, 200
    except Exception as e:
        db.session.rollback()
        # Attempt to rollback rename if DB fails? Complex. For now, log and error.
        current_app.logger.error(f"Error renaming file {file_id}: {str(e)}")
        # If os.rename succeeded but DB failed, we have a discrepancy.
        # A more robust solution might try to rename back or use a two-phase commit pattern.
        return {"error": f"Failed to rename file: {str(e)}"}, 500

# ────────────── Auth & password flows (unchanged) ───────────────────────
@app.route('/register', methods=['POST'])
def register():
    allowed, retry = hit_rate_limit(f"ratelimit:register:ip:{request.remote_addr}",
                                    *REGISTER_LIMIT_PER_IP)
    if not allowed:
        return _too_many_attempts(retry)

    data = request.get_json()
    if not data or not data.get('username') or not data.get('password') or not data.get('email'):
        return jsonify({"message": "Username, email, and password are required"}), 400

    if User.query.filter_by(username=data['username']).first() or \
       User.query.filter_by(email=data['email']).first():
        return jsonify({"message": "User already exists"}), 400

    user = User(username=data['username'], email=data['email'], grade=data['grade'])
    user.set_password(data['password'])
    db.session.add(user)
    db.session.commit()

    # create root folder + disk dir
    root = Folder(name='Root folder', owner_id=user.id, parent_id=None)
    db.session.add(root); db.session.commit()
    os.makedirs(os.path.join(Config.UPLOAD_FOLDER, user.username),
                exist_ok=True)

    return {"message": "Registered successfully."}

@app.route('/session-status', methods=['GET'])
def session_status():
    """Get current session status and user info"""
    if current_user.is_authenticated:
        return jsonify({
            "authenticated": True,
            "user": {
                "id": current_user.id,
                "username": current_user.username,
                "email": current_user.email,
                "is_admin": current_user.is_admin
            }
        })
    else:
        return jsonify({"authenticated": False, "user": None})

@app.route('/login', methods=['POST'])
def login():
    data = request.json or {}
    username = data.get('username')
    password = data.get('password')
    
    if not username or not password:
        return {"error": "Username and password required"}, 400

    # Two limits, because each alone is easy to get around: per IP stops one
    # client guessing across many accounts, per username stops a distributed
    # attack on one account from many IPs.
    #
    # The attempt is counted *before* the password is checked. Counting only
    # failures means reading the counter first, and a burst of parallel
    # requests could all read it below the limit before any records a failure.
    allowed, retry = hit_rate_limit(f"ratelimit:login:ip:{request.remote_addr}",
                                    *LOGIN_LIMIT_PER_IP)
    if not allowed:
        return _too_many_attempts(retry)

    username_limit_key = f"ratelimit:login:user:{username.strip().lower()}"
    allowed, retry = hit_rate_limit(username_limit_key, *LOGIN_LIMIT_PER_USERNAME)
    if not allowed:
        return _too_many_attempts(retry)

    user = User.query.filter_by(username=username).first()
    if user and user.check_password(password):
        # A successful login clears the username counter, so a user who
        # mistyped a few times is not left close to the lockout threshold.
        reset_rate_limit(username_limit_key)

        # Force logout any existing session first
        if current_user.is_authenticated:
            logout_user()
        
        login_user(user, remember=False, fresh=True)
        return jsonify({
            "message": "Login successful",
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "is_admin": user.is_admin
            }
        })
    return {"error": "Invalid credentials"}, 401

@app.route('/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return {"message": "Logged out"}

@app.route('/request-reset', methods=['POST'])
def request_reset():
    email = (request.json or {}).get('email', '')

    # Without a limit this endpoint can flood an inbox with reset mails. The
    # per-email limit applies whether or not the address exists, so the 429
    # reveals nothing about which e-mails are registered.
    allowed, retry = hit_rate_limit(f"ratelimit:reset:ip:{request.remote_addr}",
                                    *RESET_LIMIT_PER_IP)
    if not allowed:
        return _too_many_attempts(retry)
    allowed, retry = hit_rate_limit(f"ratelimit:reset:email:{email.strip().lower()}",
                                    *RESET_LIMIT_PER_EMAIL)
    if not allowed:
        return _too_many_attempts(retry)

    user  = User.query.filter_by(email=email).first()
    if user:
        tok = generate_reset_token(user)
        print("🔒 reset link:", f"{request.host_url}reset-password/{tok}")
    return {"message": "If that e-mail exists, a reset link was sent"}

@app.route('/reset-password/<token>', methods=['POST'])
def reset_password(token):
    user = verify_reset_token(token)
    if not user:
        return {"error": "Invalid or expired token"}, 400
    new_pwd = request.json.get('password', '')
    if len(new_pwd) < 6:
        return {"error": "Password too short"}, 400
    user.set_password(new_pwd); db.session.commit()
    return {"message": "Password updated"}

@app.route('/change-password', methods=['POST'])
@login_required
def change_password():
    data = request.json or {}
    cur, new = data.get('current_password',''), data.get('new_password','')
    if not current_user.check_password(cur):
        return {"error":"Current password is wrong"}, 400
    current_user.set_password(new); db.session.commit()
    return {"message":"Password updated"}

@app.route('/user-info', methods=['GET'])
@login_required
def get_user_info():
    """Get current user's profile information"""
    return jsonify({
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "grade": current_user.grade,
        "is_admin": current_user.is_admin,
        "created_at": current_user.created_at.isoformat() if current_user.created_at else None
    })

# ────────────── Review System ─────────────────────────────────────────────
@app.route('/users', methods=['GET'])
@login_required
def get_users():
    """Get list of all users for reviewer assignment"""
    users = User.query.filter(User.id != current_user.id).all()
    return jsonify([{
        "id": user.id,
        "username": user.username,
        "email": user.email
    } for user in users])

@app.route('/request-review/<int:file_id>', methods=['POST'])
@login_required
def request_review(file_id):
    """Request a review for a document"""
    file = File.query.get_or_404(file_id)
    if file.owner_id != current_user.id:
        return {"error": "Access denied to file"}, 403
    
    if file.is_under_review:
        return {"error": "File is already under review"}, 400
    
    data = request.json
    reviewer_id = data.get('reviewer_id')
    if not reviewer_id:
        return {"error": "Reviewer ID is required"}, 400
    
    reviewer = User.query.get(reviewer_id)
    if not reviewer:
        return {"error": "Reviewer not found"}, 404
    
    if reviewer.id == current_user.id:
        return {"error": "Cannot assign review to yourself"}, 400
    
    try:
        # Refresh the file data to ensure we have the latest version information
        db.session.refresh(file)
        
        # Get the current version (which contains the latest changes)
        current_version = file.current_version or 1
        
        # For comparison purposes:
        # - modified_version should be the current version (with latest changes)
        # - original_version should be the previous version (before the changes)
        # If this is version 1, there's no previous version to compare
        if current_version > 1:
            original_version = current_version - 1
            modified_version = current_version
        else:
            # For the first version, we compare against nothing (no original)
            original_version = None
            modified_version = current_version
        
        # Create review record
        review = DocumentReview(
            file_id=file_id,
            reviewer_id=reviewer_id,
            requester_id=current_user.id,
            status='pending',
            original_version=original_version,
            modified_version=modified_version
        )
        db.session.add(review)
        
        # Mark file as under review
        file.is_under_review = True
        
        # Create notification for reviewer
        notification = Notification(
            user_id=reviewer_id,
            title="New Document Review Request",
            message=f"{current_user.username} has requested you to review '{file.filename}'",
            type="review_request",
            related_file_id=file_id,
            related_review_id=review.id
        )
        db.session.add(notification)
        db.session.commit()
        
        # Log the review creation for debugging
        current_app.logger.info(f"Review created for file {file_id}: original_version={original_version}, modified_version={modified_version}")
        
        return {"message": "Review request sent successfully", "original_version": original_version, "modified_version": modified_version}, 201
    except Exception as e:
        db.session.rollback()
        return {"error": f"Error creating review request: {str(e)}"}, 500

@app.route('/my-reviews', methods=['GET'])
@login_required
def get_my_reviews():
    """Get reviews assigned to current user"""
    reviews = DocumentReview.query.filter_by(reviewer_id=current_user.id).order_by(DocumentReview.requested_at.desc()).all()
    
    return jsonify([{
        "id": review.id,
        "file_id": review.file_id,
        "filename": review.file.filename,
        "requester": review.requester.username,
        "status": review.status,
        "requested_at": review.requested_at.isoformat(),
        "reviewed_at": review.reviewed_at.isoformat() if review.reviewed_at else None,
        "comments": review.comments,
        "original_version": review.original_version,
        "modified_version": review.modified_version,
        "has_comparison": review.original_version is not None and review.modified_version is not None
    } for review in reviews])

@app.route('/review/<int:review_id>', methods=['POST'])
@login_required
def submit_review(review_id):
    """Submit a review decision"""
    review = DocumentReview.query.get_or_404(review_id)
    if review.reviewer_id != current_user.id:
        return {"error": "Access denied"}, 403
    
    if review.status != 'pending':
        return {"error": "Review has already been completed"}, 400
    
    data = request.json
    decision = data.get('decision')  # 'approved' or 'rejected'
    comments = data.get('comments', '')
    
    if decision not in ['approved', 'rejected']:
        return {"error": "Decision must be 'approved' or 'rejected'"}, 400
    
    try:
        # Update review
        review.status = decision
        review.comments = comments
        review.reviewed_at = datetime.utcnow()
        
        # Update file status
        review.file.is_under_review = False
        review.file.is_published = True
        
        # Create notification for requester
        notification = Notification(
            user_id=review.requester_id,
            title=f"Review {decision.title()}",
            message=f"Your document '{review.file.filename}' has been {decision} by {current_user.username}",
            type="review_completed",
            related_file_id=review.file_id,
            related_review_id=review_id
        )
        db.session.add(notification)
        db.session.commit()
        
        return {"message": f"Review {decision} successfully"}, 200
    except Exception as e:
        db.session.rollback()
        return {"error": f"Error submitting review: {str(e)}"}, 500

@app.route('/cancel-review/<int:file_id>', methods=['POST'])
@login_required
def cancel_review(file_id):
    """Cancel a pending review request"""
    file = File.query.get_or_404(file_id)
    if file.owner_id != current_user.id:
        return {"error": "Access denied to file"}, 403
    
    review = DocumentReview.query.filter_by(file_id=file_id, status='pending').first()
    if not review:
        return {"error": "No pending review found for this file"}, 404
    
    try:
        # Update review status
        review.status = 'cancelled'
        
        # Update file status
        file.is_under_review = False
        file.is_published = False
        
        # Create notification for reviewer
        notification = Notification(
            user_id=review.reviewer_id,
            title="Review Request Cancelled",
            message=f"The review request for '{file.filename}' has been cancelled by {current_user.username}",
            type="info",
            related_file_id=file_id,
            related_review_id=review.id
        )
        db.session.add(notification)
        db.session.commit()
        
        return {"message": "Review request cancelled successfully"}, 200
    except Exception as e:
        db.session.rollback()
        return {"error": f"Error cancelling review: {str(e)}"}, 500

@app.route('/notifications', methods=['GET'])
@login_required
def get_notifications():
    """Get user's notifications"""
    notifications = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.created_at.desc()).all()
    
    return jsonify([{
        "id": notif.id,
        "title": notif.title,
        "message": notif.message,
        "type": notif.type,
        "is_read": notif.is_read,
        "created_at": notif.created_at.isoformat(),
        "related_file_id": notif.related_file_id,
        "related_review_id": notif.related_review_id
    } for notif in notifications])

@app.route('/notifications/<int:notif_id>/read', methods=['POST'])
@login_required
def mark_notification_read(notif_id):
    """Mark a notification as read"""
    notification = Notification.query.get_or_404(notif_id)
    if notification.user_id != current_user.id:
        return {"error": "Access denied"}, 403
    
    notification.is_read = True
    db.session.commit()
    
    return {"message": "Notification marked as read"}, 200

@app.route('/notifications/mark-all-read', methods=['POST'])
@login_required
def mark_all_notifications_read():
    """Mark all notifications as read"""
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update({"is_read": True})
    db.session.commit()
    
    return {"message": "All notifications marked as read"}, 200

# ────────────── Error: file too large ────────────────────────────────────
@app.errorhandler(RequestEntityTooLarge)
def too_large(_): return {"error":"File exceeds limit"}, 413

# ────────────── Version Control ─────────────────────────────────────────
@app.route('/upload-version/<int:file_id>', methods=['POST'])
@login_required
def upload_version(file_id):
    f = request.files.get('file')
    comment = request.form.get('comment', '')
    
    if not f:
        return {"error": "No file provided"}, 400

    file = File.query.get_or_404(file_id)
    if file.owner_id != current_user.id and not current_user.is_admin:
        return {"error": "Access denied"}, 403

    # Create new version
    # Get the next version number by checking the highest existing version
    new_version_number = get_next_version_number(file_id)
    version_dir = get_version_dir(current_user.username)
    os.makedirs(version_dir, exist_ok=True)
    version_path = os.path.join(version_dir, f"{file_id}_v{new_version_number}_{secure_filename(f.filename)}")
    f.save(version_path)

    # Create version record
    version = FileVersion(
        file_id=file.id,
        version_number=new_version_number,
        path=version_path,
        comment=comment
    )
    
    # Update file's current version
    file.current_version = new_version_number
    file.path = version_path  # Update current file path to latest version
    
    db.session.add(version)
    db.session.commit()
    
    return {
        "message": "New version uploaded successfully",
        "version_number": new_version_number,
        "version_id": version.id
    }, 201

@app.route('/file-versions/<int:file_id>', methods=['GET'])
@login_required
def get_file_versions(file_id):
    file = File.query.get_or_404(file_id)
    if file.owner_id != current_user.id and not current_user.is_admin:
        return {"error": "Access denied"}, 403

    versions = FileVersion.query.filter_by(file_id=file_id).order_by(FileVersion.version_number.desc()).all()
    return jsonify([{
        "version_number": v.version_number,
        "uploaded_at": v.uploaded_at.isoformat(),
        "comment": v.comment,
        "is_current": v.version_number == file.current_version,
        "size": os.path.getsize(v.path) if os.path.exists(v.path) else 0
    } for v in versions])

@app.route('/restore-version/<int:file_id>/<int:version_number>', methods=['POST'])
@login_required
def restore_version(file_id, version_number):
    if not current_user.is_admin:
        return {"error": "Admin access required to restore versions"}, 403
        
    file = File.query.get_or_404(file_id)
    # Admin can restore versions for any user, so owner_id check might be removed or adjusted based on policy
    # For now, let's assume admin can restore any file they have access to query.
    # if file.owner_id != current_user.id and not current_user.is_admin:
    #     return {"error": "Access denied"}, 403

    version = FileVersion.query.filter_by(file_id=file_id, version_number=version_number).first_or_404()
    
    # Create new version from the restored version
    new_version_number = get_next_version_number(file_id)
    
    # Determine the owner of the file to correctly place the version in their .version directory
    file_owner = User.query.get(file.owner_id)
    if not file_owner:
        return {"error": "File owner not found, cannot restore version"}, 500
    owner_username = file_owner.username
    
    version_dir = get_version_dir(owner_username) # Use owner's username
    os.makedirs(version_dir, exist_ok=True)
    new_version_path = os.path.join(version_dir, f"{file_id}_v{new_version_number}_{secure_filename(file.filename)}")
    
    # Copy the restored version to new version
    import shutil
    shutil.copy2(version.path, new_version_path)
    
    # Create new version record
    new_version = FileVersion(
        file_id=file.id,
        version_number=new_version_number,
        path=new_version_path,
        comment=f"Restored from version {version_number} by admin {current_user.username}" # Added admin info to comment
    )
    
    # Update file's current version and path
    # The main file.path should point to the live file, which is now this new version.
    # The live file itself will be a copy of this new version content.
    file.current_version = new_version_number
    # Ensure the main file.path is updated if it's not already reflecting the latest content structure
    # For simplicity, we assume file.path points to the location of the live current version.
    # We need to copy the content of new_version_path to file.path
    if file.path != new_version_path: # Avoid copying if paths are already the same (e.g. if File.path was updated by upload_version)
        shutil.copy2(new_version_path, file.path)
    
    db.session.add(new_version)
    db.session.commit()
    
    current_app.logger.info(f"Admin {current_user.username} restored file {file.id} to V{version_number} (new version created: V{new_version_number})")
    return {
        "message": f"Successfully restored version {version_number}. New version {new_version_number} created.",
        "new_version_number": new_version_number
    }

@app.route('/download-version/<int:file_id>/<int:version_number>')
@login_required
def download_version(file_id, version_number):
    file = File.query.get_or_404(file_id)
    if file.owner_id != current_user.id and not current_user.is_admin:
        return {"error": "Access denied"}, 403

    version = FileVersion.query.filter_by(file_id=file_id, version_number=version_number).first_or_404()
    
    if not os.path.exists(version.path):
        return {"error": "Version file not found"}, 404
        
    return send_from_directory(
        os.path.dirname(version.path),
        os.path.basename(version.path),
        as_attachment=True,
        download_name=f"{os.path.splitext(file.filename)[0]}_v{version_number}{os.path.splitext(file.filename)[1]}"
    )

@app.route('/version-content/<int:file_id>/<int:version_number>', methods=['GET'])
@login_required
def get_version_content(file_id, version_number):
    """Get content of a specific version for preview purposes"""
    file = File.query.get_or_404(file_id)
    if file.owner_id != current_user.id and not current_user.is_admin:
        return {"error": "Access denied"}, 403

    version = FileVersion.query.filter_by(file_id=file_id, version_number=version_number).first_or_404()
    
    if not os.path.exists(version.path):
        return {"error": "Version file not found"}, 404

    # Check if it's a text file that can be previewed
    if not file.filename.lower().endswith(('.txt', '.md', '.py', '.js', '.json', '.yaml', '.yml', '.html', '.css')):
        return {"error": "File type not supported for content preview"}, 400

    try:
        with open(version.path, 'r', encoding='utf-8') as f:
            content = f.read()
        return jsonify({
            "content": content,
            "filename": file.filename,
            "version_number": version_number,
            "mimetype": file.mimetype,
            "comment": version.comment,
            "uploaded_at": version.uploaded_at.isoformat()
        })
    except Exception as e:
        return {"error": f"Error reading version content: {str(e)}"}, 500

@app.route('/delete-version/<int:file_id>/<int:version_number>', methods=['DELETE'])
@login_required
def delete_version(file_id, version_number):
    if not current_user.is_admin:
        return {"error": "Admin access required to delete versions"}, 403

    file = File.query.get_or_404(file_id)
    # Admin can delete versions for any file.

    version = FileVersion.query.filter_by(file_id=file_id, version_number=version_number).first_or_404()
    
    # Don't allow deleting the current version, even for admins, as it can lead to inconsistencies.
    # Admin should first restore to another version if they want to delete the one currently active.
    if version.version_number == file.current_version:
        return {"error": "Cannot delete the current live version. Restore to another version first."}, 400
        
    # Don't allow deleting the only version if it's not the current one (though this case is less likely if above check is done).
    if FileVersion.query.filter_by(file_id=file_id).count() <= 1:
        return {"error": "Cannot delete the only version of a file."}, 400
    
    try:
        version_path = version.path # Store path before deleting record
        db.session.delete(version)
        db.session.commit() # Commit DB changes first
        
        # Then delete the file from disk
        if os.path.exists(version_path):
            os.remove(version_path)
        
        current_app.logger.info(f"Admin {current_user.username} deleted V{version_number} of file {file.id} from path {version_path}")
        return {"message": f"Version {version_number} deleted successfully"}
    except Exception as e:
        db.session.rollback() # Rollback DB if disk operation fails or other error
        current_app.logger.error(f"Admin {current_user.username} failed to delete V{version_number} of file {file.id}: {str(e)}", exc_info=True)
        return {"error": f"Failed to delete version: {str(e)}"}, 500

@app.route('/restore-file/<int:file_id>', methods=['POST'])
@login_required
def restore_file(file_id):
    """
    Restore a deleted file from its latest version.
    This is useful when a file was accidentally deleted but versions still exist.
    """
    # Find the latest version
    latest_version = FileVersion.query.filter_by(file_id=file_id).order_by(FileVersion.version_number.desc()).first()
    if not latest_version:
        return {"error": "No versions found for this file"}, 404

    # Check if file still exists in database
    file = File.query.get(file_id)
    if file:
        return {"error": "File still exists, no need to restore"}, 400

    # Get the original file information from the latest version
    version_path = latest_version.path
    if not os.path.exists(version_path):
        return {"error": "Version file not found"}, 404

    # Create new file record
    new_file = File(
        filename=os.path.basename(version_path).split('_', 1)[1],  # Remove version prefix
        mimetype=latest_version.file.mimetype if latest_version.file else None,
        path=version_path,
        owner_id=current_user.id,
        folder_id=latest_version.file.folder_id if latest_version.file else None,
        current_version=latest_version.version_number
    )
    
    db.session.add(new_file)
    db.session.commit()

    return {
        "message": "File restored successfully",
        "file_id": new_file.id,
        "version_number": latest_version.version_number
    }, 201

@app.route('/cleanup-versions/<int:file_id>', methods=['POST'])
@login_required
def cleanup_versions(file_id):
    """
    Clean up old versions while keeping the current version.
    This helps manage storage space.
    """
    file = File.query.get_or_404(file_id)
    if file.owner_id != current_user.id and not current_user.is_admin:
        return {"error": "Access denied"}, 403

    # Get all versions except current
    old_versions = FileVersion.query.filter_by(file_id=file_id).filter(
        FileVersion.version_number != file.current_version
    ).all()

    deleted_count = 0
    for version in old_versions:
        try:
            if os.path.exists(version.path):
                os.remove(version.path)
            db.session.delete(version)
            deleted_count += 1
        except FileNotFoundError:
            pass

    db.session.commit()
    return {
        "message": f"Cleaned up {deleted_count} old versions",
        "current_version": file.current_version
    }

@app.route('/restore-to-version/<int:file_id>/<int:version_number>', methods=['POST'])
@login_required
def restore_to_version(file_id, version_number):
    """
    Directly restore a file to a specific version, replacing the current version.
    This is different from restore-version which creates a new version.
    ADMIN ONLY.
    """
    if not current_user.is_admin:
        return {"error": "Admin access required to directly restore to a version"}, 403

    file = File.query.get_or_404(file_id)
    # Admin can perform this for any file, so owner check is implicitly covered by admin role.

    # Get the target version
    target_version = FileVersion.query.filter_by(file_id=file_id, version_number=version_number).first_or_404()
    
    if not os.path.exists(target_version.path):
        return {"error": "Version file not found"}, 404

    # No backup needed as this is an admin action, and it's a direct replacement.
    # If backup is desired, it can be added back.

    # Replace current version content with target version content
    try:
        import shutil
        # The file.path should point to the live content file.
        # We are overwriting the live content with the content of the target_version.
        shutil.copy2(target_version.path, file.path) 
        
        # Update file's current version number in the database
        file.current_version = version_number
        
        # The FileVersion.path for target_version already points to its historical content.
        # We are NOT changing FileVersion paths here. We are changing the LIVE file content (file.path)
        # and updating which version number is considered current (file.current_version).

        db.session.commit()
        
        current_app.logger.info(f"Admin {current_user.username} directly restored file {file.id} to V{version_number}. Live file content updated.")
        return {
            "message": f"Successfully restored file content to version {version_number}",
            "current_version": version_number
        }
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Admin {current_user.username} failed to restore file {file.id} to V{version_number}: {str(e)}", exc_info=True)
        return {"error": f"Failed to restore version: {str(e)}"}, 500

@app.route('/compare-versions/<int:file_id>/<int:version1>/<int:version2>', methods=['GET'])
@login_required
def compare_versions(file_id, version1, version2):
    """
    Compare two versions of a file and return their differences.
    This is useful for text files. For binary files, it will return basic metadata comparison.
    """
    file = File.query.get_or_404(file_id)
    if file.owner_id != current_user.id and not current_user.is_admin:
        return {"error": "Access denied"}, 403

    v1 = FileVersion.query.filter_by(file_id=file_id, version_number=version1).first_or_404()
    v2 = FileVersion.query.filter_by(file_id=file_id, version_number=version2).first_or_404()

    if not os.path.exists(v1.path) or not os.path.exists(v2.path):
        return {"error": "One or both version files not found"}, 404

    # Basic metadata comparison
    comparison = {
        "version1": {
            "number": v1.version_number,
            "uploaded_at": v1.uploaded_at.isoformat(),
            "comment": v1.comment,
            "size": os.path.getsize(v1.path)
        },
        "version2": {
            "number": v2.version_number,
            "uploaded_at": v2.uploaded_at.isoformat(),
            "comment": v2.comment,
            "size": os.path.getsize(v2.path)
        }
    }

    # For text files, try to show content differences
    if file.mimetype and file.mimetype.startswith('text/'):
        try:
            with open(v1.path, 'r') as f1, open(v2.path, 'r') as f2:
                content1 = f1.read()
                content2 = f2.read()
                
                # Simple line-by-line comparison
                lines1 = content1.splitlines()
                lines2 = content2.splitlines()
                
                comparison["text_differences"] = {
                    "total_lines_v1": len(lines1),
                    "total_lines_v2": len(lines2),
                    "different_lines": []
                }
                
                # Compare lines and collect differences
                for i, (line1, line2) in enumerate(zip(lines1, lines2)):
                    if line1 != line2:
                        comparison["text_differences"]["different_lines"].append({
                            "line_number": i + 1,
                            "version1": line1,
                            "version2": line2
                        })
                
                # Handle different length files
                if len(lines1) != len(lines2):
                    comparison["text_differences"]["length_difference"] = {
                        "v1_extra_lines": len(lines1) - len(lines2) if len(lines1) > len(lines2) else 0,
                        "v2_extra_lines": len(lines2) - len(lines1) if len(lines2) > len(lines1) else 0
                    }
        except Exception as e:
            comparison["text_comparison_error"] = str(e)

    return jsonify(comparison)

@app.route('/review-comparison/<int:review_id>', methods=['GET'])
@login_required
def get_review_comparison(review_id):
    """Get comparison data for a review (original vs modified content)"""
    review = DocumentReview.query.get_or_404(review_id)
    # Ensure the reviewer or the requester can access the comparison
    if review.reviewer_id != current_user.id and review.requester_id != current_user.id:
        return {"error": "Access denied"}, 403
    
    file = review.file
    if not file.filename.lower().endswith(('.txt', '.md', '.py', '.js', '.json', '.yaml', '.yml', '.html', '.css')):
        return {"error": "File comparison not supported for this file type"}, 400
    
    try:
        # Get content using the new helper function
        original_content = _get_content_for_version(db.session, file, review.original_version)
        modified_content = _get_content_for_version(db.session, file, review.modified_version)
        
        current_app.logger.info(f"Review Comparison for review {review.id}:")
        current_app.logger.info(f"  File ID: {file.id}, Filename: {file.filename}")
        current_app.logger.info(f"  Original Version: {review.original_version}, Length: {len(original_content)}")
        current_app.logger.info(f"  Modified Version: {review.modified_version}, Length: {len(modified_content)}")

        return jsonify({
            "review_id": review_id,
            "file_id": file.id,
            "filename": file.filename,
            "original_content": original_content,
            "modified_content": modified_content,
            "original_version": review.original_version,
            "modified_version": review.modified_version,
            "requester": review.requester.username,
            "requested_at": review.requested_at.isoformat()
        })
    except Exception as e:
        current_app.logger.error(f"Error loading comparison for review {review_id}: {str(e)}")
        return {"error": f"Error loading comparison: {str(e)}"}, 500

# ────────────── Admin Routes ─────────────────────────────────────
@app.route('/admin/list-users', methods=['GET'])
@login_required
def admin_list_users():
    if not current_user.is_admin:
        return {"error": "Admin access required"}, 403
    users = User.query.filter(User.id != current_user.id).all() # Exclude current admin
    return jsonify([{
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "grade": user.grade,
        "created_at": user.created_at.isoformat() if user.created_at else None
    } for user in users])

@app.route('/admin/user-files/<int:target_user_id>', methods=['GET'])
@login_required
def admin_get_user_files(target_user_id):
    if not current_user.is_admin:
        return {"error": "Admin access required"}, 403

    target_user = User.query.get(target_user_id)
    if not target_user:
        return {"error": "Target user not found"}, 404

    # Adjusted serialization function to work with target_user context
    def ser_admin(folder: Folder, for_user: User):
        return {
            "id": folder.id, "name": folder.name,
            "parent_id": folder.parent_id,
            "files": [
                {
                    "id": f.id, "name": f.filename,
                    "mimetype": f.mimetype,
                    "uploaded_at": f.uploaded_at.isoformat(),
                    "is_under_review": f.is_under_review,
                    "is_published": f.is_published,
                    "current_version": f.current_version, # Add current_version for admin view
                    "owner_id": f.owner_id, # Explicitly include owner_id
                    "active_review": {
                        "id": f.get_active_review().id,
                        "reviewer": f.get_active_review().reviewer.username,
                        "requested_at": f.get_active_review().requested_at.isoformat()
                    } if f.get_active_review() else None
                } for f in folder.files if f.owner_id == for_user.id # Filter by target user
            ],
            "children": [ser_admin(ch, for_user) for ch in folder.subfolders if ch.owner_id == for_user.id] # Filter by target user
        }
    
    # Fetch root folder for the target user
    root_folder_target_user = Folder.query.filter_by(owner_id=target_user.id, parent_id=None).first()
    
    if not root_folder_target_user:
        # If target user has no root folder (e.g., new user, or some issue)
        # Return an empty structure for their files.
        return jsonify({
            "tree": {"id": None, "name": f"{target_user.username}'s Files (Root)", "files": [], "children": []},
            "target_user": {"id": target_user.id, "username": target_user.username}
        })

    folder_tree = ser_admin(root_folder_target_user, target_user)
    
    # We don't need flat_folders for this admin view of another user's files, 
    # as moving files between users is not part of this feature yet.
    
    return jsonify({
        "tree": folder_tree,
        "target_user": {"id": target_user.id, "username": target_user.username}
    })

# ────────────── Bootstrapping ────────────────────────────────────────────
# def create_default_test_user():
#     """Create a default test user if it doesn't exist"""
#     existing_user = User.query.filter_by(username='test').first()
#     if not existing_user:
#         test_user = User(username='test', email='test@example.com')
#         test_user.set_password('test')
#         db.session.add(test_user)
#         db.session.flush()  # Get the user ID
        
#         # Create root folder for test user
#         root_folder = Folder(name='Root folder', owner_id=test_user.id, parent_id=None)
#         db.session.add(root_folder)
#         db.session.commit()
        
#         # Create user directory on disk
#         user_dir = os.path.join(Config.UPLOAD_FOLDER, 'test')
#         os.makedirs(user_dir, exist_ok=True)
        
#         # Create version directory for the user
#         version_dir = os.path.join(user_dir, '.version')
#         os.makedirs(version_dir, exist_ok=True)
        
#         print("✅ Created default test user (username: test, password: test)")
#     else:
#         print("ℹ️  Test user already exists")


@app.route('/auth/google/login')
def google_login():
    """Redirect to Google OAuth login"""
    redirect_uri = url_for('google_callback', _external=True)

    global FRONTEND_ROOT
    global FRONTEND_PORT
    print(f"Redirect URI: {redirect_uri}")
    print(f"Frontend Port: {FRONTEND_PORT}")

    if FRONTEND_PORT == 80 or FRONTEND_PORT == "80":
        if "localhost" in redirect_uri:
            FRONTEND_ROOT = f"http://localhost"

        elif "127.0.0.1" in redirect_uri:
            FRONTEND_ROOT = f"http://127.0.0.1"

        redirect_uri = "http://localhost/api/auth/google/callback"
        print(f"Redirect URI: {redirect_uri}")

        return google.authorize_redirect(redirect_uri)

    elif "localhost" in redirect_uri and "localhost:5001" not in redirect_uri:
        localhost_name = redirect_uri.split("://")[1].split("/")[0]
        redirect_uri = redirect_uri.replace(localhost_name, "localhost:5001")

        FRONTEND_ROOT = f"http://localhost:{FRONTEND_PORT}"

    elif "127.0.0.1" in redirect_uri and "127.0.0.1:5001" not in redirect_uri:
        localhost_name = redirect_uri.split("://")[1].split("/")[0]
        redirect_uri = redirect_uri.replace(localhost_name, "127.0.0.1:5001")
    
        FRONTEND_ROOT = f"http://127.0.0.1:{FRONTEND_PORT}"

    return google.authorize_redirect(redirect_uri)


@app.route('/auth/google/callback')
def google_callback():  # pragma: no cover
    """Handle Google OAuth callback"""
    google = oauth.create_client('google')  # Create a Google OAuth client
    token = google.authorize_access_token()
    user_info = google.get('userinfo').json()

    email = user_info.get('email')
    if not email:
        return {"error": "No email found in Google user info"}, 400

    user = User.query.filter_by(email=email).first()
    if user is None:
        base = email.split('@')[0]
        username = base
        i = 1
        while User.query.filter_by(username=username).first():
            username = f"{base}{i}"; i += 1

        user = User(username=username, email=email, grade=33)
        user.set_password(os.urandom(16).hex())   # SSO 帳號不用真正密碼

        db.session.add(user)
        db.session.commit()

        root = Folder(name='Root folder', owner_id=user.id, parent_id=None)
        db.session.add(root)
        db.session.commit()

        os.makedirs(os.path.join(Config.UPLOAD_FOLDER, username),
                exist_ok=True)

    # login the user
    login_user(user, remember=False, fresh=True)

    return redirect(f"{FRONTEND_ROOT}/oauth2/success")

if __name__ == '__main__':  # pragma: no cover
    os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)
    app.secret_key = os.getenv("SECRET_KEY", "dev-secret")

    # for i in range(10):
    #     try:
    #         with app.app_context():
    #             db.create_all()
    #             print("✅ tables:", inspect(db.engine).get_table_names())
                
    #             # # Create default test user after tables are created
    #             # create_default_test_user()
    #             break
    #     except OperationalError:
    #         print("⏳ waiting for MySQL…"); time.sleep(3)
    # else:
    #     print("❌ DB not reachable"); exit(1)

    app.run(host='0.0.0.0', port=80)