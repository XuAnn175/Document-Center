<!-- # Document Center -->
<p align="center">
  <img src="./assets/icon.png" width="80" alt="Document Center Icon">
</p>

<h1 align="center">Document Center</h1>

<p align="center">
  <em>A unified platform to upload, manage, version, and review enterprise documents.</em>
</p>

<p align="center">
  <a href="https://github.com/XuAnn175/Document-Center/actions/workflows/ci.yaml"><img src="https://github.com/XuAnn175/Document-Center/actions/workflows/ci.yaml/badge.svg" alt="CI"></a>
  <a href="https://github.com/XuAnn175/Document-Center/actions/workflows/cd.yaml"><img src="https://github.com/XuAnn175/Document-Center/actions/workflows/cd.yaml/badge.svg" alt="CD"></a>
  <img src="https://img.shields.io/badge/python-3.10-blue.svg" alt="Python 3.10">
  <img src="https://img.shields.io/badge/vue-3.4-42b883.svg" alt="Vue 3">
  <img src="https://img.shields.io/badge/mysql-8-4479A1.svg" alt="MySQL 8">
</p>

---

## Demo

A full walkthrough of the platform — registration, folder management, document editing, version history, and the review/approval workflow.

<p align="center">
  <a href="https://www.youtube.com/watch?v=mm9JBROTSS8">
    <img src="https://img.youtube.com/vi/mm9JBROTSS8/maxresdefault.jpg" width="720" alt="Document Center demo video">
  </a>
</p>

<p align="center">
  ▶️ <a href="https://www.youtube.com/watch?v=mm9JBROTSS8"><b>Watch the demo on YouTube</b></a>
</p>

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Tech Stack](#tech-stack)
- [Features](#features)
- [Project Structure](#project-structure)
- [Getting Started](#getting-started)
- [Service Endpoints](#service-endpoints)
- [Default Accounts](#default-accounts)
- [Configuration](#configuration)
- [API Reference](#api-reference)
- [Data Model](#data-model)
- [Testing](#testing)
- [CI/CD](#cicd)
- [Monitoring](#monitoring)
- [Troubleshooting](#troubleshooting)

---

## Overview

**Document Center** is a centralized platform designed for enterprises to manage diverse technical and production documents. It enables users to upload, organize, edit, and audit files within an integrated and secure environment.

Unlike a plain file share, every document in Document Center carries a **full version history** and must pass through a **reviewer approval workflow** before it becomes publicly visible. This makes it suitable for industries with compliance or traceability requirements, where "who changed what, when, and who signed off on it" needs to be answerable at any time.

**Core ideas:**

| Concept | Behaviour |
|---------|-----------|
| **Per-user isolation** | Every user owns a private folder tree on disk (`uploads/<username>/…`) and in the database. Cross-user access is denied unless a file is published. |
| **Immutable versions** | Each save or re-upload creates a new numbered `FileVersion` snapshot under `uploads/<username>/.version/`. Nothing is overwritten in place. |
| **Review before publish** | A file becomes `is_published` only after an assigned reviewer approves it. Cancelling a review un-publishes the file. |
| **Auditability** | Reviews record the requester, reviewer, decision, comments, timestamps, and the exact version pair that was compared. |
| **Recoverability** | Deleting a file leaves its version history behind, so it can be restored later; versions can also be individually restored, compared, or pruned. |

---

## Architecture

![architecture](./assets/arch.png)

```
                            ┌──────────────────────────┐
   Browser ───── :8080 ───▶ │  Nginx  (Vue 3 SPA)      │
                            │  /        → static files │
                            │  /api/*   → reverse proxy│
                            └───────────┬──────────────┘
                                        │ :5001
                            ┌───────────▼──────────────┐        ┌──────────────┐
                            │  Flask API               │───────▶│  MySQL 8     │
                            │  Flask-Login sessions    │        │  cloud_docs  │
                            │  SQLAlchemy ORM          │        └──────────────┘
                            │  /metrics (Prometheus)   │
                            └───────────┬──────────────┘
                                        │  local volume
                            ┌───────────▼──────────────┐
                            │  uploads/                │
                            │   └─ <username>/         │
                            │       ├─ <folders…>      │
                            │       └─ .version/       │
                            └──────────────────────────┘

   Prometheus (:9090) ──scrape──▶ backend:5001/metrics ──▶ Grafana (:3000)
   phpMyAdmin (:8081) ───────────────────────────────────▶ MySQL
```

**Request flow.** The browser talks only to Nginx. Static SPA assets are served directly; anything under `/api/` is proxied to the Flask backend, which keeps the session cookie on the same origin (no CORS issues in production). The frontend keeps a session cache and re-validates it every 10 seconds, so a login in another tab is detected and surfaced to the user.

**Storage split.** Metadata lives in MySQL; file bytes live on a mounted volume. Version snapshots are stored in a hidden `.version/` directory per user, named `<file_id>_v<n>_<filename>`, which keeps the user-visible tree clean while preserving history.

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| Frontend | Vue 3, Vue Router 4, Vite 5, Axios, Vue Quill (rich-text editor) |
| Backend | Flask, Flask-Login, Flask-CORS, SQLAlchemy, Werkzeug, Gunicorn |
| Database | MySQL 8 (SQLite in-memory for tests) |
| Auth | Session-based login (`werkzeug.security` password hashing), Google OAuth 2.0 via Authlib |
| Web server | Nginx (SPA hosting + API reverse proxy) |
| Monitoring | Prometheus, Grafana, `prometheus-flask-exporter` |
| Testing | pytest, pytest-flask, pytest-cov |
| CI/CD | GitHub Actions → GitHub Container Registry (GHCR) |
| Deployment | Docker, Docker Compose, Kubernetes (k3d), phpMyAdmin |

---

## Features

### Authentication & Accounts

- Username/password registration and login with server-side sessions (1-hour lifetime, `HttpOnly` + `SameSite=Lax` cookies).
- **Google OAuth 2.0** sign-in via Authlib and OpenID Connect discovery; first-time OAuth users are auto-provisioned.
- Password reset by one-time token (`secrets.token_urlsafe(48)`, 1-hour expiry) plus in-session password change.
- `/session-status` endpoint for the SPA's navigation guards; the router blocks unauthenticated access to `/files`, `/reviews`, `/user-info`, and `/admin-dashboard`.
- **Role-based access:** regular users see only their own tree; admins can browse every user's files via a dedicated dashboard.

### Folder & File Management

- Nested folder tree per user with create/delete and a configurable depth limit (default 10 levels).
- Upload files into any folder, download, rename, and move between folders — the physical file on disk moves with the record.
- Drag-and-drop-friendly tree components, plus a flat folder list for move-target pickers.
- Soft delete: removing a file keeps its version snapshots, so `/list-deleted-files` can surface it and `/restore-file` brings it back.
- Permanent delete for irreversible cleanup.
- Upload size cap (25 MB per request by default) with a clean `413` JSON response instead of an HTML error page.
- Optional per-user soft quota (500 MB default).

### In-Browser Editing & Version Control

- Rich-text/plain-text editor in the browser (Vue Quill) with save-back to the server.
- Every save or re-upload creates a **new immutable version** with an optional change comment.
- Version history view listing each version's number, timestamp, comment, and size.
- **Compare any two versions** — metadata diff for all files, plus line-by-line differences for text MIME types.
- **Restore** a previous version (as the new latest version, so history is never lost), download a specific version, view a version's content, or delete an individual version.
- `cleanup-versions` endpoint to prune old snapshots for a file.

### Document Review & Approval Workflow

1. An owner requests a review on a file and assigns a **reviewer** (any other user; self-review is rejected).
2. The file is flagged `is_under_review`, and the reviewer receives an in-app notification.
3. The review records the **version pair** to inspect — the current version as `modified_version` and the preceding one as `original_version` — so the reviewer sees exactly what changed.
4. The reviewer opens a side-by-side comparison and submits **approve** or **reject** with comments.
5. On approval the file becomes **published** and visible to all users through `/public-files`; the requester is notified of the outcome.
6. The owner can **cancel** a pending review at any time, which un-publishes the file and notifies the reviewer.

### Notifications

- In-app notification feed with unread counts, typed entries (`info`, `review_request`, `review_completed`), and links back to the related file and review.
- Mark-one-as-read and mark-all-as-read actions, surfaced through a persistent notification bar.

### Admin

- Admin-only dashboard listing all users and browsing any user's full folder/file tree.
- Admins can download and delete any file regardless of ownership.

### Observability

- All Flask endpoints are auto-instrumented and exposed at `/metrics`.
- Prometheus scrapes the backend every 10 seconds; Grafana is provisioned with the datasource and a starter dashboard at boot.

---

## Project Structure

```
Document-Center/
├── backend/
│   ├── app.py                     # Flask app: all 45+ REST endpoints
│   ├── models.py                  # User, Folder, File, FileVersion, DocumentReview, Notification, ResetToken
│   ├── config.py                  # Config class: paths, limits, session & DB settings
│   ├── init_db.py                 # Table creation + admin/test user bootstrap
│   ├── migrate_root_folder.py     # One-off migration: ensure every user has a root folder
│   ├── migrate_review_versions.py # One-off migration: backfill review version pairs
│   ├── requirements.txt
│   ├── Dockerfile
│   ├── .env.sample                # Google OAuth credentials template
│   └── tests/                     # pytest suite (auth, folders, versions, reviews, admin)
├── frontend/
│   ├── src/
│   │   ├── views/                 # Login, Register, Files, Reviews, UserInfo,
│   │   │                          #   ResetPassword, OAuthSuccess, AdminDashboard
│   │   ├── components/            # FolderTree, PublicFolderTree, SimpleFolderTreeView,
│   │   │                          #   TextEditor, FileVersion, RenameFileModal,
│   │   │                          #   Navbar, NotificationBar, AdminUserFileBrowser
│   │   ├── router/index.js        # Routes, navigation guards, session monitoring
│   │   ├── App.vue
│   │   └── main.js
│   ├── nginx.conf                 # SPA fallback + /api/ → backend:5001 proxy
│   ├── vite.config.js
│   ├── package.json
│   └── Dockerfile                 # Multi-stage: node build → nginx serve
├── k8s/
│   ├── deploys.yaml               # MySQL StatefulSet + PVC, backend/frontend/monitoring Deployments
│   ├── services.yaml              # ClusterIP services for every component
│   ├── ingress.yaml               # / → frontend, /grafana → Grafana
│   └── start_k8s.sh               # k3d cluster + ConfigMaps + apply manifests
├── prometheus/prometheus.yml      # Scrape config (backend:5001)
├── grafana/
│   ├── datasource.yaml            # Prometheus datasource provisioning
│   ├── dashboard.yaml             # Dashboard provider provisioning
│   ├── config.ini
│   └── dashboards/example.json
├── .github/workflows/
│   ├── ci.yaml                    # pytest + coverage report in the run summary
│   └── cd.yaml                    # Build & push backend/frontend images to GHCR
├── docker-compose.yml
└── assets/                        # Icon and architecture diagram
```

---

## Getting Started

### Prerequisites

| Tool | Needed for |
|------|-----------|
| [Docker](https://www.docker.com/) & [Docker Compose](https://docs.docker.com/compose/) | The standard local stack |
| [k3d](https://k3d.io/) & [kubectl](https://kubernetes.io/docs/tasks/tools/) | The Kubernetes deployment path |
| Python 3.10 + Node 20 | Running backend/frontend natively without containers |

### Option 1 — Docker Compose (recommended)

1. **(Optional)** Enable Google OAuth login by creating a `.env` file in `./backend`:

   ```bash
   cd ./backend
   cp .env.sample .env
   # then edit .env and fill in your own GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET
   ```

2. Build and start every service:

   ```bash
   docker-compose up --build
   ```

   Compose waits for the MySQL health check before starting the backend, and the backend creates all tables plus the bootstrap users on first boot.

3. Open the app at **[http://localhost:8080](http://localhost:8080)**.

4. Tear down (add `-v` to also drop the MySQL volume):

   ```bash
   docker-compose down
   ```

### Option 2 — Kubernetes (k3d)

```bash
cd k8s
./start_k8s.sh
```

The script creates a k3d cluster named `doccen` (API on port 6666, load balancer on port 80), generates ConfigMaps from the Prometheus and Grafana configs, applies the deployments/services/ingress, and then watches the pods until they are ready.

Access the app at **[http://localhost](http://localhost)** and Grafana at **[http://localhost/grafana](http://localhost/grafana)**.

```bash
# useful while things start up
kubectl get pods -w
kubectl logs -f deploy/backend
kubectl delete cluster doccen   # or: k3d cluster delete doccen
```

### Option 3 — Local development (no containers)

```bash
# --- backend ---
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL="mysql+pymysql://flaskuser:flaskpass@localhost:3307/cloud_docs"
export FLASK_APP=app.py PYTHONPATH=$PWD
flask run --host=0.0.0.0 --port=5001 --with-threads

# --- frontend (separate shell) ---
cd frontend
npm install
npm run dev        # Vite dev server on http://localhost:5173
```

Start just the database with `docker-compose up mysql` if you don't have MySQL installed locally.

---

## Service Endpoints

| Service | Docker Compose | Kubernetes |
|---------|----------------|------------|
| Frontend (Vue SPA) | [localhost:8080](http://localhost:8080) | [localhost](http://localhost) |
| Backend API (Flask) | [localhost:5001](http://localhost:5001) | `backend:5001` (cluster-internal) |
| Prometheus metrics | [localhost:5001/metrics](http://localhost:5001/metrics) | `backend:5001/metrics` |
| Prometheus UI | [localhost:9090](http://localhost:9090) | `prometheus:9090` |
| Grafana | [localhost:3000](http://localhost:3000) | [localhost/grafana](http://localhost/grafana) |
| phpMyAdmin | [localhost:8081](http://localhost:8081) | `phpmyadmin:8081` |
| MySQL | `localhost:3307` → container `3306` | `mysql:3306` |

---

## Default Accounts

Created automatically on first backend boot by `init_db.create_admin_and_test_users`:

| Username | Password | Role |
|----------|----------|------|
| `admin` | `admin123` | Administrator |
| `testuser` | `test` | Regular user (with a pre-created root folder) |

> ⚠️ These are development conveniences. Change or remove them before any real deployment.

---

## Configuration

### Backend environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | `mysql+pymysql://flaskuser:flaskpass@host.docker.internal/cloud_docs` | SQLAlchemy connection string |
| `SECRET_KEY` | `dev-secret-key` | Flask session signing key |
| `GOOGLE_CLIENT_ID` | `your-google-client-id` | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | `your-google-client-secret` | Google OAuth client secret |
| `FRONTEND_PORT` | `80` | Used to build the OAuth post-login redirect target |
| `FLASK_APP` | `app.py` | Flask entrypoint |
| `PYTHONPATH` | `/app/backend` | Required for the package-relative imports |

### Application limits (`backend/config.py`)

| Setting | Default | Meaning |
|---------|---------|---------|
| `UPLOAD_FOLDER` | `uploads` | Root of on-disk file storage |
| `MAX_CONTENT_LENGTH` | 25 MB | Maximum size of a single upload |
| `USER_FOLDER_QUOTA_MB` | 500 MB | Soft per-user storage quota |
| `FOLDER_DEPTH_LIMIT` | 10 | Maximum folder nesting depth |
| `PERMANENT_SESSION_LIFETIME` | 3600 s | Session expiry |
| `SESSION_COOKIE_SECURE` | `False` | Set to `True` when serving over HTTPS |

---

## API Reference

All endpoints except `/`, `/register`, `/login`, `/request-reset`, `/reset-password/<token>`, and the Google OAuth routes require an authenticated session. Behind Nginx these are reachable under the `/api/` prefix.

### Health & Auth

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Health check |
| `POST` | `/register` | Create an account |
| `POST` | `/login` | Log in and start a session |
| `POST` | `/logout` | End the session |
| `GET` | `/session-status` | Whether the session is valid, plus the current user |
| `POST` | `/request-reset` | Issue a password-reset token |
| `POST` | `/reset-password/<token>` | Consume a reset token and set a new password |
| `POST` | `/change-password` | Change password while logged in |
| `GET` | `/user-info` | Current user's profile |
| `GET` | `/users` | List users (for reviewer selection) |
| `GET` | `/auth/google/login` | Begin the Google OAuth flow |
| `GET` | `/auth/google/callback` | OAuth callback / redirect into the SPA |

### Folders & Files

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/folders` | Current user's folder tree (nested and flat forms) |
| `POST` | `/folders` | Create a folder |
| `DELETE` | `/folders/<fid>` | Delete a folder |
| `POST` | `/upload` | Upload a file into a folder (creates version 1) |
| `GET` | `/download/<file_id>` | Download the current file |
| `POST` | `/move-file` | Move a file to another folder |
| `POST` | `/rename-file/<file_id>` | Rename a file |
| `DELETE` | `/delete/<file_id>` | Delete a file (history retained) |
| `DELETE` | `/permanently-delete/<file_id>` | Irreversibly delete a file and its versions |
| `GET` | `/list-deleted-files` | Deleted files that can still be restored |
| `POST` | `/restore-file/<file_id>` | Restore a deleted file from its history |
| `GET` | `/public-files` | All published (approved) files across users |
| `GET` | `/file-content/<file_id>` | Read text content for the editor |
| `POST` | `/file-content/<file_id>` | Save edited content (creates a new version) |

### Versions

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/file-versions/<file_id>` | List all versions of a file |
| `POST` | `/upload-version/<file_id>` | Upload a new version of an existing file |
| `GET` | `/version-content/<file_id>/<n>` | Read the content of version *n* |
| `GET` | `/download-version/<file_id>/<n>` | Download version *n* |
| `POST` | `/restore-version/<file_id>/<n>` | Restore version *n* |
| `POST` | `/restore-to-version/<file_id>/<n>` | Roll the file back to version *n* |
| `DELETE` | `/delete-version/<file_id>/<n>` | Delete a single version |
| `POST` | `/cleanup-versions/<file_id>` | Prune old versions |
| `GET` | `/compare-versions/<file_id>/<v1>/<v2>` | Metadata + line-level diff between two versions |

### Reviews & Notifications

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/request-review/<file_id>` | Request a review and assign a reviewer |
| `GET` | `/my-reviews` | Reviews assigned to the current user |
| `POST` | `/review/<review_id>` | Submit `approved` / `rejected` with comments |
| `POST` | `/cancel-review/<file_id>` | Cancel a pending review request |
| `GET` | `/review-comparison/<review_id>` | Version diff attached to a review |
| `GET` | `/notifications` | Current user's notifications |
| `POST` | `/notifications/<id>/read` | Mark one notification as read |
| `POST` | `/notifications/mark-all-read` | Mark all notifications as read |

### Admin

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/admin/list-users` | All users (admin only) |
| `GET` | `/admin/user-files/<user_id>` | Any user's folder/file tree (admin only) |

### Metrics

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/metrics` | Prometheus exposition format (auto-instrumented) |

---

## Data Model

```
User ──1:N──▶ Folder ──self-referencing──▶ Folder (subfolders)
  │              │
  │              └──1:N──▶ File ──1:N──▶ FileVersion
  │                          │
  │                          └──1:N──▶ DocumentReview
  └──1:N──▶ Notification, ResetToken
```

| Table | Key columns |
|-------|-------------|
| `user` | `username`, `email`, `password_hash`, `grade`, `is_admin`, `created_at` |
| `folder` | `name`, `owner_id`, `parent_id` (self-FK for nesting), `created_at` |
| `file` | `filename`, `mimetype`, `path`, `owner_id`, `folder_id`, `current_version`, `is_under_review`, `is_published` |
| `file_version` | `file_id`, `version_number`, `path`, `comment`, `uploaded_at` — unique on (`file_id`, `version_number`) |
| `document_review` | `file_id`, `reviewer_id`, `requester_id`, `status`, `comments`, `original_version`, `modified_version`, `requested_at`, `reviewed_at` |
| `notification` | `user_id`, `title`, `message`, `type`, `is_read`, `related_file_id`, `related_review_id` |
| `reset_token` | `user_id`, `token` (unique), `expires_at` |

Cascade deletes remove a file's versions and reviews with it. Indexes cover review status/reviewer/file, notification user/read state, and reset-token lookups.

### Migrations

Two one-off scripts are provided for upgrading existing databases:

```bash
python -m backend.migrate_root_folder      # ensure every user has a root folder
python -m backend.migrate_review_versions  # backfill original/modified version pairs on reviews
```

---

## Testing

The suite runs against an in-memory SQLite database with a temporary upload directory, so it needs no MySQL and leaves nothing behind.

```bash
cd backend
pytest                                              # run everything
pytest --cov=./ --cov-report=term-missing           # with coverage
pytest --cov=./ --cov-report=html                   # HTML report → htmlcov/index.html
pytest tests/test_review.py -v                      # a single module
```

| Test module | Covers |
|-------------|--------|
| `test_auth.py` | Health check, register/login/logout, Google OAuth entry point |
| `test_folder_files.py` | Folder creation, upload, move, rename, delete |
| `test_version.py` | Saving content generates a version; restore flow |
| `test_reset_tok_ver.py` | Reset-token issue/verify/expiry lifecycle |
| `test_review.py` | Full request → review → complete workflow |
| `test_admin.py` | Admin privileges and admin version flow |

---

## CI/CD

**CI — [`.github/workflows/ci.yaml`](.github/workflows/ci.yaml)** runs on every push and pull request:

1. Set up Python 3.10 and install `backend/requirements.txt`.
2. Run `pytest` with coverage (term, HTML, and XML reports).
3. Upload the HTML report as a `coverage-data` artifact.
4. Parse the report and write a **per-file coverage table plus total coverage** into the GitHub Actions run summary.

**CD — [`.github/workflows/cd.yaml`](.github/workflows/cd.yaml)** runs on pushes to `main`:

1. Log in to **GHCR** using the `GHCR_TOKEN` secret.
2. Build and push `ghcr.io/<owner>/doc-backend:latest`.
3. Build and push `ghcr.io/<owner>/doc-backend-frontend:latest`.

The image repository name is lowercased automatically, since GHCR rejects uppercase paths. The Kubernetes manifests pull these published images.

---

## Monitoring

- **Instrumentation** — `prometheus-flask-exporter` wraps the Flask app and exposes request counts, latency histograms, and status-code breakdowns per endpoint at `/metrics`.
- **Prometheus** — scrapes `backend:5001` every 10 seconds ([`prometheus/prometheus.yml`](prometheus/prometheus.yml)).
- **Grafana** — the Prometheus datasource and dashboard provider are provisioned at startup, with a starter dashboard in [`grafana/dashboards/example.json`](grafana/dashboards/example.json). Useful panels to build on it:
  - API request rate and error rate per endpoint
  - Upload/download throughput and latency percentiles
  - Review workflow volume (requests created vs. completed)
  - Container/system health

In Kubernetes, Grafana is exposed through the ingress at `/grafana` and its config is mounted from ConfigMaps generated by `start_k8s.sh`.

---

## Troubleshooting

| Symptom | Cause & fix |
|---------|-------------|
| Backend exits at boot with an `OperationalError` | MySQL wasn't ready. Compose has a health check gate; if you started the backend by hand, wait for MySQL and retry. |
| `413 File exceeds limit` | The upload is above `MAX_CONTENT_LENGTH` (25 MB). Raise it in `backend/config.py`. |
| Login succeeds but the next request is `401` | The session cookie isn't reaching the API. Use the app through Nginx (port 8080) rather than calling port 5001 from a different origin. |
| Google login fails | `backend/.env` is missing or holds placeholder credentials, and the redirect URI must be registered in the Google Cloud console. |
| "Session changed from X to Y" prompt | Expected — the SPA polls `/session-status` every 10 s and warns when another tab logs in as a different user. |
| Port already allocated | Change the host-side port mappings in `docker-compose.yml` (8080, 5001, 3307, 8081, 9090, 3000). |
| Uploaded files vanish after `docker-compose down -v` | `-v` drops the MySQL volume; the `uploads/` bind mount lives under `./backend`. |

---

<p align="center">
  <sub>Built with Vue 3, Flask, and MySQL · Deployed with Docker & Kubernetes · Monitored with Prometheus & Grafana</sub>
</p>
