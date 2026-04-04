# Self-Healing SQL Agent

A FastAPI + vanilla JS SQL agent that connects to SQLite, DuckDB, PostgreSQL, and MySQL, generates SQL from natural language, and retries automatically when a query fails.

This repo is now set up for:

- Firebase Authentication on the frontend
- Railway-first deployment for the full app
- Optional Firebase Hosting or Cloud Run deployment if you want to split frontend/backend later

## Stack

- Frontend: static HTML/CSS/JS in [`static/index.html`](/Users/shaiksameer/Documents/self-healing-sql-agent/static/index.html)
- Auth: Firebase Auth (Google and email/password)
- Backend: FastAPI + Uvicorn in [`server.py`](/Users/shaiksameer/Documents/self-healing-sql-agent/server.py)
- AI: Groq via `llama-3.3-70b-versatile`
- Deployment: Railway for the full app, or Cloud Run/Firebase Hosting if you want to split services

## Local Development

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Create env vars

Copy [`.env.example`](/Users/shaiksameer/Documents/self-healing-sql-agent/.env.example) into `.env` and fill in what you need.

Minimum for local backend work:

```bash
GROQ_API_KEY=your_groq_api_key
```

If you want Firebase auth enabled locally, also set:

```bash
FIREBASE_PROJECT_ID=your-firebase-project-id
FIREBASE_SERVICE_ACCOUNT_PATH=/absolute/path/to/service-account.json
FIREBASE_API_KEY=your_web_api_key
FIREBASE_AUTH_DOMAIN=your-project.firebaseapp.com
FIREBASE_STORAGE_BUCKET=your-project.firebasestorage.app
FIREBASE_MESSAGING_SENDER_ID=1234567890
FIREBASE_APP_ID=1:1234567890:web:abcdef123456
```

### 3. Run the backend

```bash
uvicorn server:app --reload
```

Open [http://localhost:8000](http://localhost:8000).

If Firebase config is not set, the app falls back to a local dev mode and skips auth.

## Recommended Deployment: Railway

Railway is the simplest production path for this project because the same FastAPI app can serve:

- the frontend from [`static/index.html`](/Users/shaiksameer/Documents/self-healing-sql-agent/static/index.html)
- the REST API
- the WebSocket endpoint

That means you do not need Firebase Hosting for deployment. You only use Firebase for authentication.

### Railway setup

1. Push this repo to GitHub.
2. In Railway, create a new project from that GitHub repo.
3. Railway will detect [`railway.toml`](/Users/shaiksameer/Documents/self-healing-sql-agent/railway.toml) and [`Dockerfile`](/Users/shaiksameer/Documents/self-healing-sql-agent/Dockerfile).
4. Add these Railway environment variables:

```bash
GROQ_API_KEY=your_groq_api_key
FIREBASE_PROJECT_ID=sql-agent-5b660
FIREBASE_SERVICE_ACCOUNT_JSON={"type":"service_account",...}
FIREBASE_API_KEY=AIzaSyCFk409Xzi93Fv9gWSuBj0ZXt99rO-0sJY
FIREBASE_AUTH_DOMAIN=sql-agent-5b660.firebaseapp.com
FIREBASE_STORAGE_BUCKET=sql-agent-5b660.firebasestorage.app
FIREBASE_MESSAGING_SENDER_ID=1030798533018
FIREBASE_APP_ID=1:1030798533018:web:de634384b6bda613ec1a23
DATA_DIR=/data
```

5. If you want SQLite or DuckDB files to persist, attach a Railway volume mounted at `/data`.
6. Deploy.

### Railway notes

- [`static/firebase-config.js`](/Users/shaiksameer/Documents/self-healing-sql-agent/static/firebase-config.js) intentionally leaves `apiBaseUrl` and `wsBaseUrl` empty so the frontend uses the same Railway domain as the backend.
- For Railway, prefer `FIREBASE_SERVICE_ACCOUNT_JSON` instead of `FIREBASE_SERVICE_ACCOUNT_PATH` because Railway secrets are easier to manage as environment variables than as files.
- If you plan to connect to Redshift or other external databases, the outbound network path is decided by Railway, not Firebase.

## Firebase Authentication Setup

Use the official Firebase console flow:

1. Create a Firebase project.
2. Add a Web app to that project.
3. In Authentication, enable:
   - Google
   - Email/Password
4. Copy the web app config values into either:
   - `.env` for same-origin local/backend serving, or
   - [`static/firebase-config.js`](/Users/shaiksameer/Documents/self-healing-sql-agent/static/firebase-config.js) for Firebase Hosting

The frontend uses Firebase Auth on the client and sends the Firebase ID token to the backend. The backend verifies that token with `firebase-admin`.

Official docs used:

- [Firebase Auth: Google sign-in](https://firebase.google.com/docs/auth/web/google-signin)
- [Firebase Auth: email/password](https://firebase.google.com/docs/auth/web/password-auth)

## Deployment Architecture

Firebase Hosting is a good fit for the static frontend, but this app also needs:

- Python execution
- database drivers
- REST endpoints
- WebSockets

Because of that, the clean setup is:

1. Deploy the frontend from `static/` to Firebase Hosting.
2. Deploy the FastAPI backend separately, typically to Cloud Run.
3. Point the frontend at that backend using [`static/firebase-config.js`](/Users/shaiksameer/Documents/self-healing-sql-agent/static/firebase-config.js).

## Optional: Frontend Deployment With Firebase Hosting

[`firebase.json`](/Users/shaiksameer/Documents/self-healing-sql-agent/firebase.json) is already included and publishes the `static/` directory.

Update [`static/firebase-config.js`](/Users/shaiksameer/Documents/self-healing-sql-agent/static/firebase-config.js):

```js
window.APP_CONFIG = {
  firebase: {
    apiKey: "your_web_api_key",
    authDomain: "your-project.firebaseapp.com",
    projectId: "your-project-id",
    storageBucket: "your-project.firebasestorage.app",
    messagingSenderId: "1234567890",
    appId: "1:1234567890:web:abcdef123456",
  },
  apiBaseUrl: "https://your-backend-service.run.app",
  wsBaseUrl: "https://your-backend-service.run.app",
};
```

Then deploy:

```bash
npm install -g firebase-tools
firebase login
firebase use <your-firebase-project-id>
firebase deploy --only hosting
```

Or use the helper script after your backend is live:

```bash
BACKEND_URL=https://your-backend-service.run.app ./scripts/deploy_frontend.sh
```

## Optional: Backend Deployment With Cloud Run

The repo already includes a [`Dockerfile`](/Users/shaiksameer/Documents/self-healing-sql-agent/Dockerfile), so Cloud Run is a straightforward option.

Example:

```bash
gcloud run deploy sql-agent-api \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars GROQ_API_KEY=... \
  --set-env-vars FIREBASE_PROJECT_ID=your-firebase-project-id
```

Or use the helper script:

```bash
GROQ_API_KEY=your_groq_api_key ./scripts/deploy_backend.sh
```

For non-Google environments, also provide either:

- `FIREBASE_SERVICE_ACCOUNT_PATH`
- `FIREBASE_SERVICE_ACCOUNT_JSON`

If you deploy on Google Cloud, `FIREBASE_PROJECT_ID` is usually enough for token verification as long as the service can access Google credentials.

## Important Files

- [`server.py`](/Users/shaiksameer/Documents/self-healing-sql-agent/server.py): FastAPI API, WebSocket query streaming, Firebase token verification
- [`static/index.html`](/Users/shaiksameer/Documents/self-healing-sql-agent/static/index.html): app UI and Firebase Auth client flow
- [`static/firebase-config.js`](/Users/shaiksameer/Documents/self-healing-sql-agent/static/firebase-config.js): frontend Firebase + backend endpoint config
- [`railway.toml`](/Users/shaiksameer/Documents/self-healing-sql-agent/railway.toml): Railway deployment config
- [`Dockerfile`](/Users/shaiksameer/Documents/self-healing-sql-agent/Dockerfile): container used by Railway and Cloud Run
- [`firebase.json`](/Users/shaiksameer/Documents/self-healing-sql-agent/firebase.json): Firebase Hosting config
- [`scripts/deploy_backend.sh`](/Users/shaiksameer/Documents/self-healing-sql-agent/scripts/deploy_backend.sh): deploy the FastAPI backend to Cloud Run
- [`scripts/deploy_frontend.sh`](/Users/shaiksameer/Documents/self-healing-sql-agent/scripts/deploy_frontend.sh): update frontend backend URLs and deploy Firebase Hosting
- [`.env.example`](/Users/shaiksameer/Documents/self-healing-sql-agent/.env.example): backend env template

## Verification

The Python files compile successfully with:

```bash
python3 -m py_compile server.py main.py agent.py database.py
```
