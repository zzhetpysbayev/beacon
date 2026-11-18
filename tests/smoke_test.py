"""End-to-end smoke test for Beacon. Needs an empty PostgreSQL database in DATABASE_URL.

Run from the project root: PYTHONPATH=. python tests/smoke_test.py
"""
import http.server
import sys
import re
import threading
import time

from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.config import settings

# Local target site: returns a page containing a marker word.
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<html>hello OK-MARK world</html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 8765), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()

PASSWORD = "Secret123!"
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


with TestClient(app, follow_redirects=False) as client:  # runs lifespan: init_db + scheduler
    # 1. login with the owner created by manage.py
    r = client.post("/login", data={"username": "admin", "password": PASSWORD})
    check("login redirects to dashboard", r.status_code == 303 and r.headers["location"] == "/")
    r = client.get("/")
    check("dashboard renders for owner", r.status_code == 200 and "Monitors" in r.text)
    check("header shows org name", "Example Org" in r.text)

    # 2. wrong password rejected
    bad = TestClient(app, follow_redirects=False)
    r = bad.post("/login", data={"username": "admin", "password": "nope"})
    check("wrong password gives 401", r.status_code == 401)
    check("protected page redirects anonymous to login", bad.get("/").headers.get("location") == "/login")

    # 3. contact + Telegram webhook binding
    client.post("/contacts", data={"name": "Ops team"})
    contacts = db.list_contacts(1)
    check("contact created unverified", len(contacts) == 1 and not contacts[0]["verified"])
    code = contacts[0]["bind_code"]
    r = client.get("/contacts")
    check("bind link shown when bot username set", f"start={code}" in r.text or not settings.telegram_bot_username)

    webhook = f"/telegram/webhook/{settings.telegram_webhook_secret}"
    r = client.post(webhook, json={"message": {"text": f"/start {code}", "chat": {"id": 555001}}})
    check("webhook accepts bind", r.status_code == 200)
    contact = db.list_contacts(1)[0]
    check("contact verified with chat_id", contact["verified"] and contact["chat_id"] == "555001")
    check("bind code cleared after use", contact["bind_code"] is None)
    r = client.post("/telegram/webhook/wrong-secret", json={"message": {}})
    check("webhook rejects wrong secret", r.status_code == 404)

    # 4. monitor that should succeed, attached to the contact
    r = client.post("/monitors", data={
        "name": "Local OK", "url": "http://127.0.0.1:8765/", "method": "GET",
        "expected_status": "200", "keyword": "OK-MARK", "timeout_s": "5",
        "interval_s": "30", "verify_ssl": "on", "enabled": "on",
        "contact_ids": [str(contact["id"])],
    })
    check("monitor created", r.status_code == 303)
    ok_id = int(r.headers["location"].rsplit("/", 1)[1])

    # 5. monitor with a wrong keyword (should be a failure)
    r = client.post("/monitors", data={
        "name": "Keyword miss", "url": "http://127.0.0.1:8765/", "method": "GET",
        "expected_status": "200", "keyword": "NOT-THERE", "timeout_s": "5",
        "interval_s": "30", "enabled": "on",
    })
    miss_id = int(r.headers["location"].rsplit("/", 1)[1])

    # 6. validation errors re-render with 400
    r = client.post("/monitors", data={"name": "", "url": "ftp://x", "interval_s": "5"})
    check("invalid monitor rejected with 400", r.status_code == 400 and "URL must" in r.text)

    # 7. wait for the scheduler to run both checks
    time.sleep(4)
    ok_checks = db.recent_checks(ok_id)
    check("scheduler wrote a successful check", len(ok_checks) >= 1 and ok_checks[0]["ok"] == 1)
    miss_checks = db.recent_checks(miss_id)
    check("keyword miss recorded as failure", len(miss_checks) >= 1 and miss_checks[0]["ok"] == 0)
    check("keyword error message stored", "Keyword not found" in (miss_checks[0]["error"] or ""))

    r = client.get(f"/monitors/{ok_id}")
    check("monitor detail renders", r.status_code == 200 and "Local OK" in r.text)
    check("uptime shown", "100.00%" in r.text)

    r = client.get("/api/monitors")
    names = {m["name"]: m for m in r.json()}
    check("api lists monitors for org", "Local OK" in names and names["Local OK"]["state"] == "up")

    # 8. contacts are attached to the monitor
    check("monitor-contact link stored", db.monitor_contact_ids(ok_id) == [contact["id"]])
    check("alert chats resolved for monitor", db.verified_chat_ids_for_monitor(ok_id) == ["555001"])

    # 9. invite a member
    r = client.post("/invites")
    check("invite page shows link", r.status_code == 200 and "/invite/" in r.text)
    token = re.search(r"/invite/([A-Za-z0-9_\-]+)", r.text).group(1)

    member = TestClient(app, follow_redirects=False)
    r = member.get(f"/invite/{token}")
    check("invite page opens for anonymous user", r.status_code == 200 and "Example Org" in r.text)
    r = member.post(f"/invite/{token}", data={"username": "colleague", "password": PASSWORD,
                                               "password2": PASSWORD})
    check("invite accepted and logged in", r.status_code == 303)
    check("member sees same monitors", "Local OK" in member.get("/").text)
    check("member cannot create invites", member.post("/invites").status_code == 403)
    check("member cannot delete contacts", member.post(f"/contacts/{contact['id']}/delete").status_code == 403)
    r = member.post(f"/invite/{token}", data={"username": "other", "password": PASSWORD, "password2": PASSWORD})
    check("used invite cannot be reused", r.status_code == 400)

    # 10. tenant isolation: second organization must not see org 1
    import subprocess
    venv_py = sys.executable
    out = subprocess.run([venv_py, "manage.py", "create-owner", "--org", "Other Co",
                          "--username", "other_owner", "--password", PASSWORD],
                         capture_output=True, text=True, check=True).stdout
    check("second org created via manage.py", "Other Co" in out)
    other = TestClient(app, follow_redirects=False)
    other.post("/login", data={"username": "other_owner", "password": PASSWORD})
    check("other org dashboard hides monitors", "Local OK" not in other.get("/").text)
    check("other org gets 404 for foreign monitor", other.get(f"/monitors/{ok_id}").status_code == 404)
    check("other org cannot toggle foreign monitor", other.post(f"/monitors/{ok_id}/toggle").status_code == 404)
    check("other org gets 404 deleting foreign contact", other.post(f"/contacts/{contact['id']}/delete").status_code == 404)
    check("foreign contact still exists after attempt", any(c["id"] == contact["id"] for c in db.list_contacts(1)))

    # 11. attaching a foreign contact to own monitor is ignored
    other.post("/contacts", data={"name": "Their contact"})
    foreign = db.list_contacts(2)[0]
    r = other.post("/monitors", data={"name": "Mine", "url": "http://127.0.0.1:8765/",
                                      "expected_status": "200", "interval_s": "60",
                                      "contact_ids": [str(contact["id"])]})
    mine_id = int(r.headers["location"].rsplit("/", 1)[1])
    check("foreign contact id dropped on link", db.monitor_contact_ids(mine_id) == [])

    # 12. delete monitor
    r = client.post(f"/monitors/{miss_id}/delete")
    check("monitor deleted", db.get_monitor(1, miss_id) is None)

srv.shutdown()
passed = sum(1 for _, ok in results if ok)
print(f"\n{passed}/{len(results)} checks passed")
raise SystemExit(0 if passed == len(results) else 1)
