import os
import secrets
from datetime import date, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import bcrypt as _bcrypt
from sqlmodel import Field, SQLModel, Session, create_engine, select
import resend


# ---------------------------------------------------------------------------
# EMAIL SETUP
# RESEND_API_KEY is set in Render's environment variables — never in code.
# ---------------------------------------------------------------------------
resend.api_key = os.environ.get("RESEND_API_KEY", "")

# ── CHANGE EMAIL SETTINGS HERE ───────────────────────────────────────────────
FROM_EMAIL    = "onboarding@resend.dev"   # swap for your verified domain later
FROM_NAME     = "Sleepker"
EMAIL_SUBJECT = "Your Weekly Sleep Summary 💤"
# ─────────────────────────────────────────────────────────────────────────────


# ---------------------------------------------------------------------------
# PASSWORD HASHING
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()

def check_password(password: str, hashed: str) -> bool:
    return _bcrypt.checkpw(password.encode(), hashed.encode())


# ---------------------------------------------------------------------------
# DATABASE MODELS
# ---------------------------------------------------------------------------

class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    # ── SETTINGS ─────────────────────────────────────────────────────────────
    email_notifications: bool = Field(default=True)
    # Set to False if the user turns off weekly emails in their settings.


class UserSession(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    token: str = Field(unique=True, index=True)


class DailyLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    date: date
    resting_hr: int
    sleep_hours: float
    soreness: int
    energy: int
    mood: int
    cardio_minutes: int = Field(default=0)
    alcohol_use: bool = Field(default=False)
    naps:        bool = Field(default=False)
    notes: Optional[str] = Field(default=None)
    # ── ADD MORE HABITS HERE ─────────────────────────────────────────────────
    # Example: meditation: bool = Field(default=False)


# ---------------------------------------------------------------------------
# DATABASE SETUP
# ---------------------------------------------------------------------------
engine = create_engine("sqlite:///recovery.db")
SQLModel.metadata.create_all(engine)

# Migration: add email_notifications column to existing databases that
# were created before this field existed.
from sqlalchemy import text
with Session(engine) as _s:
    try:
        _s.exec(text("ALTER TABLE user ADD COLUMN email_notifications BOOLEAN DEFAULT 1"))
        _s.commit()
    except Exception:
        pass
    try:
        _s.exec(text("ALTER TABLE dailylog ADD COLUMN notes TEXT"))
        _s.commit()
    except Exception:
        pass  # column already exists — safe to ignore


# ---------------------------------------------------------------------------
# INPUT MODELS
# ---------------------------------------------------------------------------

class UserCreate(SQLModel):
    username: str
    password: str


class CheckInCreate(SQLModel):
    date: date
    resting_hr: int
    sleep_hours: float
    soreness: int
    energy: int
    mood: int
    cardio_minutes: int = 0
    alcohol_use: bool = False
    naps:        bool = False
    notes: Optional[str] = None
    # ── ADD MORE HABITS HERE ─────────────────────────────────────────────────


class SettingsUpdate(SQLModel):
    email_notifications: bool


# ---------------------------------------------------------------------------
# AUTH HELPER
# ---------------------------------------------------------------------------

def get_current_user(authorization: str = Header(...)) -> User:
    token = authorization.replace("Bearer ", "").strip()
    with Session(engine) as session:
        user_session = session.exec(
            select(UserSession).where(UserSession.token == token)
        ).first()
        if not user_session:
            raise HTTPException(status_code=401, detail="Invalid or expired token.")
        user = session.get(User, user_session.user_id)
        if not user:
            raise HTTPException(status_code=401, detail="User not found.")
        return user


# ---------------------------------------------------------------------------
# EMAIL HELPERS
# ---------------------------------------------------------------------------

def compute_score(log: DailyLog) -> float:
    return round(
        (log.sleep_hours / 9 * 100) * 0.30 +
        ((6 - log.soreness) / 5 * 100) * 0.25 +
        (log.energy / 5 * 100) * 0.20 +
        (log.mood / 5 * 100) * 0.15 +
        (min(log.cardio_minutes, 90) / 90 * 100) * 0.10
    )


def build_email_html(username: str, logs: list) -> str:
    if not logs:
        return f"""
        <div style="font-family:sans-serif;max-width:560px;margin:0 auto;padding:32px;color:#1e293b">
          <h2 style="color:#2563eb">Your Weekly Sleep Summary</h2>
          <p>Hi {username},</p>
          <p>No check-ins logged this week. Open the app and start tracking — even one entry a day makes a difference.</p>
          <a href="https://sleep-tracker-cmw0.onrender.com/checkin"
             style="display:inline-block;margin-top:16px;padding:12px 24px;background:#2563eb;color:#fff;border-radius:8px;text-decoration:none;font-weight:600">
            Log Today's Check-In
          </a>
          <hr style="margin:40px 0;border:none;border-top:1px solid #e2e8f0"/>
          <p style="color:#94a3b8;font-size:13px">
            Thank you for using Sleepker. Sleep well and recover strong.<br/>
            <a href="https://sleep-tracker-cmw0.onrender.com" style="color:#2563eb">sleepker.onrender.com</a>
            · <a href="https://sleep-tracker-cmw0.onrender.com/checkin" style="color:#64748b">Manage email settings</a>
          </p>
        </div>"""

    scores      = [compute_score(l) for l in logs]
    avg_score   = round(sum(scores) / len(scores))
    best_log    = logs[scores.index(max(scores))]
    worst_log   = logs[scores.index(min(scores))]
    avg_sleep   = round(sum(l.sleep_hours for l in logs) / len(logs), 1)
    avg_hr      = round(sum(l.resting_hr  for l in logs) / len(logs))

    # ── WEEKLY EMAIL TEMPLATE ─────────────────────────────────────────────────
    # To change the design, edit the HTML below.
    # Variables available: username, avg_score, best_log.date, worst_log.date,
    #                      avg_sleep, avg_hr, len(logs)
    # ─────────────────────────────────────────────────────────────────────────
    return f"""
    <div style="font-family:sans-serif;max-width:560px;margin:0 auto;padding:32px;color:#1e293b">
      <h2 style="color:#2563eb;margin-bottom:4px">Your Weekly Sleep Summary</h2>
      <p style="color:#64748b;margin-top:0">Hi {username}, here's how your week looked.</p>

      <div style="background:#f4f9fd;border:1px solid #d6e8f5;border-radius:12px;padding:24px;margin:24px 0">
        <div style="font-size:48px;font-weight:800;color:#2563eb;text-align:center">{avg_score}</div>
        <div style="text-align:center;color:#64748b;font-size:14px">Average Readiness Score (out of 100)</div>
      </div>

      <table style="width:100%;border-collapse:collapse;margin-bottom:24px">
        <tr>
          <td style="padding:12px;border-bottom:1px solid #e2e8f0;color:#64748b;font-size:14px">Check-ins logged</td>
          <td style="padding:12px;border-bottom:1px solid #e2e8f0;font-weight:600;text-align:right">{len(logs)} / 7 days</td>
        </tr>
        <tr>
          <td style="padding:12px;border-bottom:1px solid #e2e8f0;color:#64748b;font-size:14px">Average sleep</td>
          <td style="padding:12px;border-bottom:1px solid #e2e8f0;font-weight:600;text-align:right">{avg_sleep} hrs</td>
        </tr>
        <tr>
          <td style="padding:12px;border-bottom:1px solid #e2e8f0;color:#64748b;font-size:14px">Average resting HR</td>
          <td style="padding:12px;border-bottom:1px solid #e2e8f0;font-weight:600;text-align:right">{avg_hr} bpm</td>
        </tr>
        <tr>
          <td style="padding:12px;border-bottom:1px solid #e2e8f0;color:#64748b;font-size:14px">Best day</td>
          <td style="padding:12px;border-bottom:1px solid #e2e8f0;font-weight:600;color:#16a34a;text-align:right">{best_log.date}</td>
        </tr>
        <tr>
          <td style="padding:12px;color:#64748b;font-size:14px">Toughest day</td>
          <td style="padding:12px;font-weight:600;color:#dc2626;text-align:right">{worst_log.date}</td>
        </tr>
      </table>

      <a href="https://sleep-tracker-cmw0.onrender.com/checkin"
         style="display:inline-block;padding:12px 24px;background:#2563eb;color:#fff;border-radius:8px;text-decoration:none;font-weight:600">
        Log This Week →
      </a>

      <hr style="margin:40px 0;border:none;border-top:1px solid #e2e8f0"/>

      <!-- ── THANK YOU MESSAGE — edit the text below ── -->
      <p style="color:#64748b;font-size:13px;line-height:1.6">
        Thank you for using Sleepker. Every check-in is a step toward understanding your body better.
        Sleep well, recover strong, and keep showing up. 💙<br/><br/>
        <a href="https://sleep-tracker-cmw0.onrender.com" style="color:#2563eb">sleepker.onrender.com</a>
        · <a href="https://sleep-tracker-cmw0.onrender.com/checkin" style="color:#64748b">Manage email settings</a>
      </p>
    </div>"""


# ---------------------------------------------------------------------------
# APP + ROUTES
# ---------------------------------------------------------------------------
app = FastAPI(title="Sleepker API")

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def read_root():
    return FileResponse("static/index.html")

@app.get("/checkin")
def read_checkin():
    return FileResponse("static/checkin.html")


# ── REGISTER ─────────────────────────────────────────────────────────────────
@app.post("/api/register")
def register(data: UserCreate):
    with Session(engine) as session:
        existing = session.exec(select(User).where(User.username == data.username)).first()
        if existing:
            raise HTTPException(status_code=400, detail="Username already taken.")
        if len(data.password.encode()) > 72:
            raise HTTPException(status_code=400, detail="Password must be 72 characters or fewer.")
        user = User(username=data.username, password_hash=hash_password(data.password))
        session.add(user)
        session.commit()
        return {"message": "Account created. You can now log in."}


# ── LOGIN ─────────────────────────────────────────────────────────────────────
@app.post("/api/login")
def login(data: UserCreate):
    with Session(engine) as session:
        user = session.exec(select(User).where(User.username == data.username)).first()
        if not user or not check_password(data.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid username or password.")
        token = secrets.token_hex(32)
        session.add(UserSession(user_id=user.id, token=token))
        session.commit()
        return {"token": token, "username": user.username}


# ── LOGOUT ────────────────────────────────────────────────────────────────────
@app.post("/api/logout")
def logout(authorization: str = Header(...)):
    token = authorization.replace("Bearer ", "").strip()
    with Session(engine) as session:
        user_session = session.exec(select(UserSession).where(UserSession.token == token)).first()
        if user_session:
            session.delete(user_session)
            session.commit()
    return {"message": "Logged out."}


# ── CHECK-IN ──────────────────────────────────────────────────────────────────
@app.post("/api/checkin")
def create_checkin(data: CheckInCreate, authorization: str = Header(...)):
    user = get_current_user(authorization)
    log  = DailyLog(**data.dict(), user_id=user.id)
    with Session(engine) as session:
        session.add(log)
        session.commit()
        session.refresh(log)
        return log


# ── GET CHECK-INS ─────────────────────────────────────────────────────────────
@app.get("/api/checkins")
def get_checkins(authorization: str = Header(...)):
    user = get_current_user(authorization)
    with Session(engine) as session:
        logs = session.exec(select(DailyLog).where(DailyLog.user_id == user.id)).all()
        results = []
        for log in logs:
            results.append({**log.dict(), "readiness_score": compute_score(log)})
        return results


# ── STATS ─────────────────────────────────────────────────────────────────────
@app.get("/api/stats")
def get_stats():
    with Session(engine) as session:
        count = len(session.exec(select(User)).all())
        return {"total_users": count}


# ── SETTINGS ──────────────────────────────────────────────────────────────────
@app.get("/api/settings")
def get_settings(authorization: str = Header(...)):
    user = get_current_user(authorization)
    return {"email_notifications": user.email_notifications}

@app.put("/api/settings")
def update_settings(data: SettingsUpdate, authorization: str = Header(...)):
    user = get_current_user(authorization)
    with Session(engine) as session:
        db_user = session.get(User, user.id)
        db_user.email_notifications = data.email_notifications
        session.commit()
    return {"email_notifications": data.email_notifications}


# ── WEEKLY REPORT (called by cron job every Sunday 9AM) ───────────────────────
# Protected by CRON_SECRET env var so only your cron job can trigger it.
# To test manually: POST /api/weekly-report?secret=YOUR_CRON_SECRET
@app.post("/api/weekly-report")
def send_weekly_report(secret: str = Query(...)):
    cron_secret = os.environ.get("CRON_SECRET", "")
    if not cron_secret or secret != cron_secret:
        raise HTTPException(status_code=401, detail="Unauthorized.")

    one_week_ago = date.today() - timedelta(days=7)
    sent = 0

    with Session(engine) as session:
        users = session.exec(select(User).where(User.email_notifications == True)).all()

        for user in users:
            logs = session.exec(
                select(DailyLog)
                .where(DailyLog.user_id == user.id)
                .where(DailyLog.date >= one_week_ago)
            ).all()

            try:
                resend.Emails.send({
                    "from":    f"{FROM_NAME} <{FROM_EMAIL}>",
                    "to":      [user.username],
                    "subject": EMAIL_SUBJECT,
                    "html":    build_email_html(user.username, list(logs)),
                })
                sent += 1
            except Exception as e:
                print(f"Failed to send to {user.username}: {e}")

    return {"sent": sent}
