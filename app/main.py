"""Web UI, JSON API, Telegram webhook and application entry point."""
import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, db
from .config import settings
from .notify import esc, send
from .scheduler import scheduler_loop

log = logging.getLogger("monitor.app")
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")
LOCAL_TZ = ZoneInfo(settings.timezone)


def _dt(value):
    dt = db.parse_ts(value)
    return "-" if dt is None else dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _pct(value):
    return "-" if value is None else f"{value:.2f}%"


def _ms(value):
    return "-" if value is None else f"{value:.0f} ms"


templates.env.filters.update(dt=_dt, pct=_pct, ms=_ms)

STATE_LABELS = {"up": "Up", "down": "Down", "unknown": "No data", "paused": "Paused"}
UPTIME_WINDOWS = (("24 h", 24), ("7 days", 24 * 7), ("30 days", 24 * 30))
DEFAULTS = {
    "name": "", "url": "", "method": "GET", "expected_status": 200, "keyword": "",
    "timeout_s": 10, "interval_s": 60, "verify_ssl": 1, "enabled": 1, "contact_ids": [],
}
ROLE_OWNER = "owner"


class LoginRequired(Exception):
    pass


def _bootstrap_initial_owner() -> None:
    """Create the first organization and owner from env, only if no users exist yet.

    Runs on every start, but does nothing once any user is in the database,
    so it is safe to leave the variables set. The password is never logged.
    """
    s = settings
    if not (s.initial_org_name and s.initial_admin_username and s.initial_admin_password):
        return
    if db.count_users() > 0:
        return
    if len(s.initial_admin_password) < 8:
        raise RuntimeError("INITIAL_ADMIN_PASSWORD must be at least 8 characters long")
    db.create_organization_with_owner(
        s.initial_org_name,
        s.initial_admin_username,
        auth.hash_password(s.initial_admin_password),
    )
    log.info("Created initial owner %r in organization %r", s.initial_admin_username, s.initial_org_name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.secret_key:
        raise RuntimeError("Set the SECRET_KEY environment variable")
    if not settings.database_url:
        raise RuntimeError("Set the DATABASE_URL environment variable")
    db.init_db()
    _bootstrap_initial_owner()
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler_loop(stop))
    yield
    stop.set()
    await task


app = FastAPI(
    title="Beacon",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key or "unset",
    same_site="lax",
    https_only=settings.cookie_secure,
    max_age=60 * 60 * 12,
)


@app.exception_handler(LoginRequired)
async def _redirect_to_login(request: Request, exc: LoginRequired):
    return RedirectResponse("/login", status_code=303)


def current_member(request: Request) -> dict:
    uid = request.session.get("uid")
    if not uid:
        raise LoginRequired()
    member = db.get_membership(uid)
    if not member:
        request.session.clear()
        raise LoginRequired()
    return member


def require_owner(member: dict) -> None:
    if member["role"] != ROLE_OWNER:
        raise HTTPException(status_code=403, detail="Insufficient permissions")


def render(request: Request, name: str, user: dict | None, status_code: int = 200, **ctx):
    return templates.TemplateResponse(
        request, name, {"user": user, **ctx}, status_code=status_code
    )


def _enrich(m: dict, now) -> dict:
    m = dict(m)
    m["state"] = m["status"] if m["enabled"] else "paused"
    m["state_label"] = STATE_LABELS[m["state"]]
    expires = db.parse_ts(m["ssl_expires_at"])
    m["ssl_days"] = (expires - now).days if expires else None
    return m


def _monitor_or_404(org_id: int, monitor_id: int) -> dict:
    m = db.get_monitor(org_id, monitor_id)
    if not m:
        raise HTTPException(status_code=404, detail="Monitor not found")
    return m


def _contact_ids_from(form) -> list[int]:
    return [int(v) for v in form.getlist("contact_ids") if str(v).isdigit()]


# --- auth ------------------------------------------------------------------

@app.get("/login")
def login_page(request: Request):
    return render(request, "login.html", None, error=None)


@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form("")):
    row = db.get_user_by_name(username.strip())
    stored = row["password_hash"] if row else auth.DUMMY_HASH
    ok = auth.verify_password(password, stored)
    if not row or not ok:
        return render(request, "login.html", None, status_code=401,
                      error="Invalid username or password")
    request.session.clear()
    request.session["uid"] = row["id"]
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- dashboard -------------------------------------------------------------

@app.get("/")
def dashboard(request: Request, member: dict = Depends(current_member)):
    org = member["organization_id"]
    now = db.utcnow()
    monitors = []
    for m in db.list_monitors(org):
        item = _enrich(m, now)
        item["uptime_24h"] = db.uptime(m["id"], 24)
        monitors.append(item)

    summary = {"total": len(monitors), "up": 0, "down": 0, "unknown": 0, "paused": 0}
    for m in monitors:
        summary[m["state"]] += 1
    return render(request, "dashboard.html", member, monitors=monitors,
                  summary=summary, warn_days=settings.ssl_warn_days)


# --- monitor forms ---------------------------------------------------------

def _parse_form(form) -> tuple[dict, dict]:
    def text(key: str) -> str:
        return str(form.get(key, "") or "").strip()

    data = {
        "name": text("name"),
        "url": text("url"),
        "method": text("method").upper() or "GET",
        "keyword": text("keyword") or None,
        "verify_ssl": 1 if form.get("verify_ssl") else 0,
        "enabled": 1 if form.get("enabled") else 0,
    }
    errors: dict[str, str] = {}

    if not 1 <= len(data["name"]) <= 100:
        errors["name"] = "Name: 1 to 100 characters"
    if not (data["url"].startswith(("http://", "https://")) and len(data["url"]) <= 2048):
        errors["url"] = "URL must start with http:// or https://"
    if data["method"] not in {"GET", "HEAD", "POST"}:
        errors["method"] = "Method: GET, HEAD or POST"
    if data["keyword"] and data["method"] == "HEAD":
        errors["keyword"] = "Keyword check does not work with the HEAD method"
    if data["keyword"] and len(data["keyword"]) > 200:
        errors["keyword"] = "Keyword: at most 200 characters"

    try:
        status = int(text("expected_status") or 200)
        if not 100 <= status <= 599:
            raise ValueError
        data["expected_status"] = status
    except ValueError:
        errors["expected_status"] = "Expected status: a number from 100 to 599"

    try:
        timeout = float(text("timeout_s") or 10)
        if not 1 <= timeout <= 60:
            raise ValueError
        data["timeout_s"] = timeout
    except ValueError:
        errors["timeout_s"] = "Timeout: 1 to 60 seconds"

    try:
        interval = int(text("interval_s") or 60)
        if not 30 <= interval <= 86400:
            raise ValueError
        data["interval_s"] = interval
    except ValueError:
        errors["interval_s"] = "Interval: 30 to 86400 seconds"

    return data, errors


@app.get("/monitors/new")
def new_monitor_page(request: Request, member: dict = Depends(current_member)):
    org = member["organization_id"]
    return render(request, "monitor_form.html", member, m=DEFAULTS, errors={},
                  title="New monitor", action="/monitors",
                  contacts=db.list_contacts(org))


@app.post("/monitors")
async def create_monitor_route(request: Request, member: dict = Depends(current_member)):
    org = member["organization_id"]
    form = await request.form()
    data, errors = _parse_form(form)
    contact_ids = db.valid_contact_ids(org, _contact_ids_from(form))
    if errors:
        return render(request, "monitor_form.html", member, status_code=400,
                      m={**data, "contact_ids": contact_ids}, errors=errors,
                      title="New monitor", action="/monitors",
                      contacts=db.list_contacts(org))
    monitor_id = db.create_monitor(org, data, contact_ids)
    return RedirectResponse(f"/monitors/{monitor_id}", status_code=303)


@app.get("/monitors/{monitor_id}/edit")
def edit_monitor_page(monitor_id: int, request: Request, member: dict = Depends(current_member)):
    org = member["organization_id"]
    m = _monitor_or_404(org, monitor_id)
    m["contact_ids"] = db.monitor_contact_ids(monitor_id)
    return render(request, "monitor_form.html", member, m=m, errors={},
                  title="Edit monitor", action=f"/monitors/{monitor_id}",
                  contacts=db.list_contacts(org))


@app.post("/monitors/{monitor_id}")
async def update_monitor_route(monitor_id: int, request: Request,
                               member: dict = Depends(current_member)):
    org = member["organization_id"]
    _monitor_or_404(org, monitor_id)
    form = await request.form()
    data, errors = _parse_form(form)
    contact_ids = db.valid_contact_ids(org, _contact_ids_from(form))
    if errors:
        return render(request, "monitor_form.html", member, status_code=400,
                      m={**data, "contact_ids": contact_ids}, errors=errors,
                      title="Edit monitor", action=f"/monitors/{monitor_id}",
                      contacts=db.list_contacts(org))
    db.update_monitor(org, monitor_id, data, contact_ids)
    return RedirectResponse(f"/monitors/{monitor_id}", status_code=303)


# --- monitor detail and actions --------------------------------------------

@app.get("/monitors/{monitor_id}")
def monitor_detail(monitor_id: int, request: Request, member: dict = Depends(current_member)):
    org = member["organization_id"]
    m = _enrich(_monitor_or_404(org, monitor_id), db.utcnow())
    uptime = [(label, db.uptime(monitor_id, hours)) for label, hours in UPTIME_WINDOWS]
    return render(request, "monitor_detail.html", member, m=m, uptime=uptime,
                  checks=db.recent_checks(monitor_id), warn_days=settings.ssl_warn_days)


@app.post("/monitors/{monitor_id}/toggle")
def toggle_monitor(monitor_id: int, member: dict = Depends(current_member)):
    org = member["organization_id"]
    m = _monitor_or_404(org, monitor_id)
    db.set_enabled(org, monitor_id, not m["enabled"])
    return RedirectResponse(f"/monitors/{monitor_id}", status_code=303)


@app.post("/monitors/{monitor_id}/check")
def check_now(monitor_id: int, member: dict = Depends(current_member)):
    _monitor_or_404(member["organization_id"], monitor_id)
    # Clearing last_checked_at makes the scheduler run it on the next tick.
    db.update_state(monitor_id, {"last_checked_at": None})
    return RedirectResponse(f"/monitors/{monitor_id}", status_code=303)


@app.post("/monitors/{monitor_id}/delete")
def delete_monitor_route(monitor_id: int, member: dict = Depends(current_member)):
    org = member["organization_id"]
    _monitor_or_404(org, monitor_id)
    db.delete_monitor(org, monitor_id)
    return RedirectResponse("/", status_code=303)


# --- alert contacts --------------------------------------------------------

@app.get("/contacts")
def contacts_page(request: Request, member: dict = Depends(current_member)):
    contacts = db.list_contacts(member["organization_id"])
    for c in contacts:
        if c["bind_code"] and settings.telegram_bot_username:
            c["bind_link"] = f"https://t.me/{settings.telegram_bot_username}?start={c['bind_code']}"
        else:
            c["bind_link"] = None
    return render(request, "contacts.html", member, contacts=contacts,
                  bot_configured=bool(settings.telegram_bot_username))


@app.post("/contacts")
async def create_contact_route(request: Request, member: dict = Depends(current_member)):
    require_owner(member)
    form = await request.form()
    name = str(form.get("name", "") or "").strip()
    if 1 <= len(name) <= 100:
        db.create_contact(member["organization_id"], name)
    return RedirectResponse("/contacts", status_code=303)


@app.post("/contacts/{contact_id}/delete")
def delete_contact_route(contact_id: int, member: dict = Depends(current_member)):
    require_owner(member)
    if not db.delete_contact(member["organization_id"], contact_id):
        raise HTTPException(status_code=404, detail="Contact not found")
    return RedirectResponse("/contacts", status_code=303)


# --- team and invites ------------------------------------------------------

def _team_page(request: Request, member: dict, new_link: str | None = None):
    return render(request, "team.html", member,
                  members=db.list_members(member["organization_id"]),
                  invites=db.list_invites(member["organization_id"]),
                  new_link=new_link)


@app.get("/team")
def team_page(request: Request, member: dict = Depends(current_member)):
    return _team_page(request, member)


@app.post("/invites")
def create_invite_route(request: Request, member: dict = Depends(current_member)):
    require_owner(member)
    token = secrets.token_urlsafe(24)
    expires = db.utcnow() + timedelta(days=settings.invite_days)
    db.create_invite(member["organization_id"], token, "member", member["user_id"], expires)
    link = f"{str(request.base_url).rstrip('/')}/invite/{token}"
    return _team_page(request, member, new_link=link)


@app.get("/invite/{token}")
def invite_page(token: str, request: Request):
    invite = db.get_invite(token)
    return render(request, "invite.html", None, invite=invite, error=None)


@app.post("/invite/{token}")
async def accept_invite_route(token: str, request: Request):
    form = await request.form()
    username = str(form.get("username", "") or "").strip()
    password = str(form.get("password", "") or "")
    password2 = str(form.get("password2", "") or "")
    invite = db.get_invite(token)

    error = None
    if not invite:
        error = "Invite is invalid or has expired"
    elif not 3 <= len(username) <= 64:
        error = "Username: 3 to 64 characters"
    elif len(password) < 8:
        error = "Password: at least 8 characters"
    elif password != password2:
        error = "Passwords do not match"

    if error is None:
        ok, error = db.accept_invite(token, username, auth.hash_password(password))
        if ok:
            request.session.clear()
            request.session["uid"] = db.get_user_by_name(username)["id"]
            return RedirectResponse("/", status_code=303)

    return render(request, "invite.html", None, status_code=400, invite=invite, error=error)


# --- Telegram webhook ------------------------------------------------------

@app.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if not settings.telegram_webhook_secret or not secrets.compare_digest(
        secret, settings.telegram_webhook_secret
    ):
        raise HTTPException(status_code=404)
    try:
        update = await request.json()
    except ValueError:
        return {"ok": True}
    if not isinstance(update, dict):
        return {"ok": True}

    message = update.get("message") or {}
    text = message.get("text") or ""
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id and text.startswith("/start ") and len(text.split(maxsplit=1)) == 2:
        code = text.split(maxsplit=1)[1].strip()
        name = db.bind_contact(code, chat_id)
        if name:
            await send(f"Connected: alerts for '{esc(name)}' will be sent to this chat.",
                       [str(chat_id)])
    return {"ok": True}


# --- JSON API (session-authenticated) --------------------------------------

@app.get("/api/monitors")
def api_monitors(request: Request, member: dict = Depends(current_member)):
    now = db.utcnow()
    result = []
    for m in db.list_monitors(member["organization_id"]):
        item = _enrich(m, now)
        result.append({
            "id": item["id"],
            "name": item["name"],
            "url": item["url"],
            "state": item["state"],
            "last_checked_at": item["last_checked_at"],
            "last_response_ms": item["last_response_ms"],
            "ssl_days": item["ssl_days"],
            "uptime_24h": db.uptime(m["id"], 24),
        })
    return JSONResponse(result)
