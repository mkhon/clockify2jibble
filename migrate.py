#!/usr/bin/env python3
"""Migrate Clockify clients, projects, tasks, members (with hourly rates) and time entries to Jibble.

Tasks become Jibble activities named "Project: Task". Billable flags, tags and
workspace/project/task/per-entry rates are not migrated: Jibble has no field for them.

  migrate.py export            read Clockify into export.json (read-only)
  migrate.py plan              compare with Jibble, write plan.json (read-only)
  migrate.py apply [--limit N] [--only-person EMAIL]
                               create what plan.json lists; resumable via state.json

Credentials come from .env next to this file (or the environment):
  CLOCKIFY_API_KEY, CLOCKIFY_WORKSPACE_ID (optional if only one), CLOCKIFY_BASE_URL (optional)
  JIBBLE_CLIENT_ID, JIBBLE_CLIENT_SECRET
"""
import collections, csv, hashlib, json, os, shutil, ssl, sys, time, uuid, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
EXPORT, PLAN, STATE, SKIPPED, CACHE = (os.path.join(HERE, f) for f in
                                        ("export.json", "plan.json", "state.json", "skipped.csv", ".cache"))
MIN_DATE = "2020-01-01"  # Jibble rejects older time entries (time_entry_before_minimum_date)
NS = uuid.UUID("6f1c2b1e-8a43-4f0e-9a3b-2c6e5d1a7b90")  # deterministic Jibble ids from Clockify ids

JIBBLE_ID = "https://identity.prod.jibble.io/connect/token"
JIBBLE_WS = "https://workspace.prod.jibble.io/v1"
JIBBLE_TT = "https://time-tracking.prod.jibble.io/v1"
# MacPorts/python.org builds may ship without a CA bundle; fall back to the macOS one.
SSL_CTX = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or
                                     ("/etc/ssl/cert.pem" if os.path.exists("/etc/ssl/cert.pem") else None))
PLATFORM = {"clientVersion": "clockify-migration 1.0", "os": "script",
            "deviceModel": "migration", "deviceName": "clockify-import"}


def load_env():
    p = os.path.join(HERE, ".env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# Write to a temp file and rename, so an interrupt never leaves a truncated JSON file behind.
def write_json(path, obj, **kw):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, **kw)
    os.replace(tmp, path)


def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def hours(start, end):
    return (parse_ts(end) - parse_ts(start)).total_seconds() / 3600


def need(name):
    v = os.environ.get(name)
    if not v:
        sys.exit(f"missing {name} (set it in {os.path.join(HERE, '.env')})")
    return v


def http(method, url, headers=None, body=None, form=False):
    data = None
    headers = dict(headers or {})
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
    headers.setdefault("Accept", "application/json")
    limited = 0  # 429s wait out the quota window (Clockify free plan: 30 requests/hour)
    attempt = 0
    while attempt < 8:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as r:
                raw = r.read()
                return r.status, dict(r.headers), (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            if e.code == 429 and limited < 40:
                limited += 1
                # Clockify's free quota is hourly; Jibble's limit is per second.
                backoff = min(2 ** limited, 30) if "jibble.io" in url else min(60 * limited, 600)
                wait = float(e.headers.get("Retry-After") or backoff)
                hdrs = {k: v for k, v in e.headers.items() if "rate" in k.lower() or k.lower() == "retry-after"}
                print(f"  429 on {method} {url.split('?')[0]} {hdrs or ''}, waiting {wait:.0f}s", file=sys.stderr, flush=True)
                time.sleep(wait)
                continue
            if e.code in (502, 503, 504):
                attempt += 1
                time.sleep(min(2 ** attempt, 60))
                continue
            return e.code, dict(e.headers), raw
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            attempt += 1
            wait = min(2 ** attempt, 60)
            print(f"  network error on {method} {url.split('?')[0]}: {e}, retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
    return 599, {}, "gave up after retries"


# ---------------------------------------------------------------- Clockify

class Clockify:
    def __init__(self):
        self.base = (os.environ.get("CLOCKIFY_BASE_URL") or "https://api.clockify.me/api/v1").rstrip("/")
        self.h = {"X-Api-Key": need("CLOCKIFY_API_KEY")}

    def get(self, path, **q):
        return self.get_h(path, **q)[0]

    # Responses are cached on disk so a restarted export does not spend the hourly quota again;
    # a completed export clears the cache, so the next one fetches fresh data.
    def get_h(self, path, **q):
        url = f"{self.base}{path}?{urllib.parse.urlencode(q)}"
        cache = os.path.join(CACHE, hashlib.sha1(url.encode()).hexdigest() + ".json")
        if os.path.exists(cache):
            c = json.load(open(cache))
            return c["body"], c["lastPage"]
        st, h, body = http("GET", url, self.h)
        if st != 200:
            sys.exit(f"Clockify GET {path} -> {st}: {body}")
        last = {k.lower(): v for k, v in h.items()}.get("last-page")
        os.makedirs(CACHE, exist_ok=True)
        write_json(cache, {"url": url, "body": body, "lastPage": last})
        return body, last

    def pages(self, path, size=200, **q):
        page = 1
        while True:
            batch, last = self.get_h(path, page=page, **{"page-size": size}, **q)
            yield from batch
            if not batch or str(last).lower() == "true":
                return
            page += 1


def cmd_export():
    c = Clockify()
    wss = c.get("/workspaces")
    ws_id = os.environ.get("CLOCKIFY_WORKSPACE_ID")
    if not ws_id:
        if len(wss) != 1:
            sys.exit("several workspaces, set CLOCKIFY_WORKSPACE_ID to one of:\n" +
                     "\n".join(f"  {w['id']}  {w['name']}" for w in wss))
        ws_id = wss[0]["id"]
    ws = next((w for w in wss if w["id"] == ws_id), None)
    if not ws:
        sys.exit(f"workspace {ws_id} not found, CLOCKIFY_WORKSPACE_ID must be one of:\n" +
                 "\n".join(f"  {w['id']}  {w['name']}" for w in wss))
    print(f"workspace {ws['name']} ({ws_id})")
    w = f"/workspaces/{ws_id}"
    users = {}
    for status in ("ACTIVE", "INACTIVE", "PENDING", "DECLINED"):
        for u in c.pages(f"{w}/users", status=status, memberships="WORKSPACE"):
            users[u["id"]] = u
    clients = list(c.pages(f"{w}/clients"))
    projects = list(c.pages(f"{w}/projects", hydrated="true"))
    tasks = []
    for p in projects:
        if "tasks" in p:
            tasks += [dict(t, projectId=t.get("projectId") or p["id"]) for t in p.pop("tasks") or []]
        else:
            tasks += list(c.pages(f"{w}/projects/{p['id']}/tasks"))
    entries = []
    for u in users.values():
        got = list(c.pages(f"{w}/user/{u['id']}/time-entries", size=1000, start="1970-01-01T00:00:00Z"))
        print(f"  {u.get('email')}: {len(got)} entries")
        entries += got
    out = {"exportedAt": datetime.now(timezone.utc).isoformat(), "workspace": ws,
           "users": list(users.values()), "clients": clients, "projects": projects, "tasks": tasks,
           "timeEntries": entries}
    write_json(EXPORT, out, indent=1)
    shutil.rmtree(CACHE, ignore_errors=True)
    print(f"{len(users)} users, {len(clients)} clients, {len(projects)} projects, {len(tasks)} tasks, "
          f"{len(entries)} entries -> {EXPORT}")


# ---------------------------------------------------------------- Jibble

class Jibble:
    def __init__(self):
        self.auth()

    # Tokens expire after about an hour; call() fetches a new one on 401.
    def auth(self):
        st, _, tok = http("POST", JIBBLE_ID, body={"grant_type": "client_credentials",
                          "client_id": need("JIBBLE_CLIENT_ID"), "client_secret": need("JIBBLE_CLIENT_SECRET")}, form=True)
        if st != 200:
            sys.exit(f"Jibble token -> {st}: {tok}")
        self.h = {"Authorization": f"Bearer {tok['access_token']}"}
        self.person = tok.get("personId")  # the admin who created the API credentials

    def call(self, method, url, body=None):
        st, h, resp = http(method, url, self.h, body)
        if st == 401:
            self.auth()
            st, h, resp = http(method, url, self.h, body)
        return st, h, resp

    def all(self, base, entity, **q):
        out, skip = [], 0
        while True:
            qs = urllib.parse.urlencode({**q, "$top": 500, "$skip": skip})
            st, _, body = self.call("GET", f"{base}/{entity}?{qs}")
            if st != 200:
                sys.exit(f"Jibble GET {entity} -> {st}: {body}")
            out += body["value"]
            if len(body["value"]) < 500:
                return out
            skip += 500

    def post(self, base, entity, body):
        return self.call("POST", f"{base}/{entity}", body)

    def patch(self, base, path, body):
        return self.call("PATCH", f"{base}/{path}", body)


def jid(kind, clockify_id):
    return str(uuid.uuid5(NS, f"{kind}:{clockify_id}"))


def shift(ts, seconds):
    t = parse_ts(ts) + timedelta(seconds=seconds)
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_skipped_csv(ex, plan, pname):
    users = {u["id"]: u for u in ex["users"]}
    clients = {c["id"]: c["name"] for c in ex["clients"]}
    pclient = {p["id"]: clients.get(p.get("clientId"), "") for p in ex["projects"]}
    tasks = {t["id"]: t["name"] for t in ex.get("tasks", [])}
    by_id = {e["id"]: e for e in ex["timeEntries"]}
    with open(SKIPPED, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["reason", "email", "name", "client", "project", "task", "start", "end", "hours",
                    "description", "billable", "hourly_rate", "currency", "clockify_id"])
        for s in sorted(plan["skipped"], key=lambda s: (by_id[s["id"]]["timeInterval"]["start"])):
            e = by_id[s["id"]]
            ti, u, r = e["timeInterval"], users.get(e["userId"], {}), e.get("hourlyRate") or {}
            w.writerow([s["reason"].split(" ending")[0], u.get("email"), u.get("name"),
                        pclient.get(e.get("projectId"), ""), pname.get(e.get("projectId"), ""),
                        tasks.get(e.get("taskId"), ""), ti["start"], ti.get("end"),
                        round(hours(ti["start"], ti["end"]), 4) if ti.get("end") else "",
                        e.get("description"), e.get("billable"),
                        r["amount"] / 100 if r.get("amount") is not None else "", r.get("currency"), e["id"]])
    print(f"skipped entries -> {SKIPPED}")


def norm(s):
    return (s or "").strip().casefold()


def cmd_plan():
    ex = json.load(open(EXPORT))
    j = Jibble()
    j_people = j.all(JIBBLE_WS, "People")
    j_clients = j.all(JIBBLE_WS, "Clients")
    j_projects = j.all(JIBBLE_WS, "Projects")
    j_acts = j.all(JIBBLE_WS, "Activities")
    st, _, orgs = j.call("GET", f"{JIBBLE_WS}/Organizations")
    if st != 200:
        sys.exit(f"Jibble GET Organizations -> {st}: {orgs}")
    org = (orgs.get("value") or [{}])[0] if "value" in orgs else orgs
    by_email = {norm(p.get("email")): p for p in j_people if p.get("email")}
    cl_by_name = {norm(c["name"]): c for c in j_clients}
    pr_by_name = {norm(p["name"]): p for p in j_projects}
    act_by_name = {norm(a["name"]): a for a in j_acts}

    ws_rate = ex["workspace"].get("hourlyRate") or {}
    plan = {"clients": [], "projects": [], "activities": [], "people": [], "entries": [], "skipped": [],
            "currency": {"clockify": ws_rate.get("currency"),
                         "jibble": org.get("IClientBillingSettings/CurrencyCode")}}
    cmap = {}
    for c in ex["clients"]:
        hit = cl_by_name.get(norm(c["name"]))
        cmap[c["id"]] = hit["id"] if hit else jid("client", c["id"])
        plan["clients"].append({"clockifyId": c["id"], "name": c["name"], "jibbleId": cmap[c["id"]],
                                "exists": bool(hit)})
    # Jibble project names are unique: on a clash the project with most entries keeps the name,
    # the others get " (Client)" appended.
    cname = {c["id"]: c["name"] for c in ex["clients"]}
    used = collections.Counter(e.get("projectId") for e in ex["timeEntries"])
    pname, by_norm = {}, collections.defaultdict(list)
    for p in ex["projects"]:
        by_norm[norm(p["name"])].append(p)
    for group in by_norm.values():
        group.sort(key=lambda p: -used[p["id"]])
        for i, p in enumerate(group):
            pname[p["id"]] = p["name"] if i == 0 else f"{p['name']} ({cname.get(p.get('clientId'), 'no client')})"
    pmap = {}
    for p in ex["projects"]:
        hit = pr_by_name.get(norm(pname[p["id"]]))
        pmap[p["id"]] = hit["id"] if hit else jid("project", p["id"])
        plan["projects"].append({"clockifyId": p["id"], "name": pname[p["id"]], "jibbleId": pmap[p["id"]],
                                 "clientId": cmap.get(p.get("clientId")), "archived": p.get("archived", False),
                                 "exists": bool(hit)})
    amap = {}
    for t in ex.get("tasks", []):
        name = f"{pname.get(t['projectId'], '?')}: {t['name']}"
        hit = act_by_name.get(norm(name))
        amap[t["id"]] = hit["id"] if hit else jid("activity", t["id"])
        plan["activities"].append({"clockifyId": t["id"], "name": name, "jibbleId": amap[t["id"]],
                                   "done": t.get("status") == "DONE", "exists": bool(hit)})

    # Jibble has one rate per person: use the rate on their latest non-zero entry. Amounts are in cents.
    latest = {}
    for e in sorted(ex["timeEntries"], key=lambda e: e["timeInterval"]["start"]):
        amount = (e.get("hourlyRate") or {}).get("amount")
        if amount:
            latest[e["userId"]] = amount

    def member_rate(u):
        return round(latest[u["id"]] / 100, 2) if u["id"] in latest else None

    umap = {}
    for u in ex["users"]:
        hit = by_email.get(norm(u.get("email")))
        umap[u["id"]] = hit["id"] if hit else jid("person", u["id"])
        plan["people"].append({"clockifyId": u["id"], "email": u.get("email"), "name": u.get("name"),
                               "clockifyStatus": u.get("status"), "jibbleId": umap[u["id"]], "exists": bool(hit),
                               "rate": member_rate(u),
                               "jibbleRate": hit.get("IPersonSetting/BillableRate") if hit else None})

    # Jibble keeps one In/Out stream per person, so overlapping Clockify entries cannot both go in.
    per_user = {}
    for e in ex["timeEntries"]:
        ti = e.get("timeInterval") or {}
        if not ti.get("end"):
            plan["skipped"].append({"id": e["id"], "reason": "still running in Clockify"})
            continue
        if e["userId"] not in umap:
            plan["skipped"].append({"id": e["id"], "reason": "user not in Clockify user list"})
            continue
        per_user.setdefault(e["userId"], []).append(e)
    for uid, es in per_user.items():
        es.sort(key=lambda e: e["timeInterval"]["start"])
        last_end = ""
        for e in es:
            s, t = e["timeInterval"]["start"], e["timeInterval"]["end"]
            if s >= t:
                plan["skipped"].append({"id": e["id"], "reason": "zero or negative duration"})
                continue
            if s < MIN_DATE:
                plan["skipped"].append({"id": e["id"], "reason": f"before {MIN_DATE} (Jibble minimum)"})
                continue
            if s < last_end:
                plan["skipped"].append({"id": e["id"], "reason": f"overlaps previous entry ending {last_end}",
                                        "start": s, "end": t, "user": uid, "description": e.get("description")})
                continue
            # Jibble rejects a clock-in at the same second as the previous clock-out.
            if s == last_end:
                s = shift(s, 1)
                if s >= t:
                    plan["skipped"].append({"id": e["id"], "reason": "zero or negative duration"})
                    continue
            last_end = t
            plan["entries"].append({"clockifyId": e["id"], "personId": umap[uid], "start": s, "end": t,
                                    "projectId": pmap.get(e.get("projectId")),
                                    "activityId": amap.get(e.get("taskId")),
                                    "note": (e.get("description") or "")[:500]})
    write_json(PLAN, plan, indent=1)
    write_skipped_csv(ex, plan, pname)

    def n(kind, exists):
        return sum(1 for x in plan[kind] if x["exists"] == exists)
    total = sum(hours(x["start"], x["end"]) for x in plan["entries"])
    print(f"clients : {n('clients', False)} to create, {n('clients', True)} already in Jibble")
    print(f"projects: {n('projects', False)} to create, {n('projects', True)} already in Jibble")
    print(f"activity: {n('activities', False)} to create from tasks, {n('activities', True)} already in Jibble")
    print(f"people  : {n('people', False)} to create (no invite email), {n('people', True)} matched by email")
    cur = plan["currency"]
    print(f"currency: Clockify {cur['clockify']}, Jibble {cur['jibble']}" +
          ("" if cur["clockify"] == cur["jibble"] else "  <- MISMATCH: rates will not be set"))
    for u in plan["people"]:
        if u["rate"] is not None:
            was = f" (Jibble now {u['jibbleRate']})" if u["exists"] else ""
            print(f"  rate {u['rate']:>8.2f}/h  {u['email']}{was}")
    print(f"entries : {len(plan['entries'])} to create, {total:.1f} h total")
    reasons = collections.Counter(s["reason"].split(" ending")[0] for s in plan["skipped"])
    for r, k in reasons.items():
        print(f"skipped : {k} - {r}")
    print(f"-> {PLAN}")


def cmd_apply(limit=None, only_email=None):
    plan = json.load(open(PLAN))
    state = json.load(open(STATE)) if os.path.exists(STATE) else {"done": {}, "errors": {}}
    j = Jibble()

    def save():
        write_json(STATE, state, indent=1)

    def create(key, base, entity, body):
        if key in state["done"]:
            return True
        st, _, resp = j.post(base, entity, body)
        # A rerun after an interruption re-sends objects created after the last save; same id = already there.
        same_id = st == 400 and "entity_save_failed_non_unique" in str(resp) and '"target":"Id"' in str(resp)
        if st in (200, 201, 204) or st == 409 or same_id:
            state["done"][key] = body.get("id")
            state["errors"].pop(key, None)
            return True
        state["errors"][key] = f"{st}: {str(resp)[:400]}"
        print(f"  FAIL {key}: {st} {str(resp)[:300]}", file=sys.stderr)
        return False

    for c in plan["clients"]:
        if not c["exists"]:
            create(f"client:{c['clockifyId']}", JIBBLE_WS, "Clients", {"id": c["jibbleId"], "name": c["name"]})
    for p in plan["projects"]:
        if not p["exists"]:
            body = {"id": p["jibbleId"], "name": p["name"]}
            if p["clientId"]:
                body["clientId"] = p["clientId"]
            create(f"project:{p['clockifyId']}", JIBBLE_WS, "Projects", body)
    for a in plan["activities"]:
        if not a["exists"]:
            create(f"activity:{a['clockifyId']}", JIBBLE_WS, "Activities", {"id": a["jibbleId"], "name": a["name"]})
    rates_ok = plan["currency"]["clockify"] == plan["currency"]["jibble"]
    for u in plan["people"]:
        rate = u["rate"] if rates_ok else None
        if not u["exists"]:
            if not u.get("email"):
                continue
            body = {"id": u["jibbleId"], "email": u["email"], "fullName": u["name"] or u["email"],
                    "IPersonSetting/SendInviteEmail": False}
            if rate is not None:
                body["IPersonSetting/BillableRate"] = rate
            create(f"person:{u['clockifyId']}", JIBBLE_WS, "People", body)
        elif rate is not None and rate != u["jibbleRate"] and state["done"].get(f"rate:{u['clockifyId']}") != rate:
            st, _, resp = j.patch(JIBBLE_WS, f"People({u['jibbleId']})", {"IPersonSetting/BillableRate": rate})
            if st in (200, 204):
                state["done"][f"rate:{u['clockifyId']}"] = rate
                state["errors"].pop(f"rate:{u['clockifyId']}", None)
            else:
                state["errors"][f"rate:{u['clockifyId']}"] = f"{st}: {str(resp)[:400]}"
    save()

    entries = plan["entries"]
    if only_email:
        pid = next((u["jibbleId"] for u in plan["people"] if norm(u["email"]) == norm(only_email)), None)
        if not pid:
            sys.exit(f"{only_email} is not in the plan")
        entries = [e for e in entries if e["personId"] == pid]
    todo = [e for e in entries if f"out:{e['clockifyId']}" not in state["done"]]
    if limit:
        todo = todo[:limit]
    # Jibble refuses clock-ins on a project with assignees unless the person is one of them.
    assignees = {p["id"]: [a["unitId"] for a in p.get("assignees") or []]
                 for p in j.all(JIBBLE_WS, "Projects", **{"$select": "id,assignees"})}

    def assign(project_id, person_id, restrict=False):
        key = f"assign:{project_id}:{person_id}"
        units = assignees.get(project_id)
        # Leave alone: unknown project, already assigned, or open to everyone unless asked to restrict.
        if units is None or person_id in units or (not units and not restrict):
            state["errors"].pop(key, None)
            return True
        # The PATCH replaces the whole list, so re-read it first: another run may have changed it.
        st, _, cur = j.call("GET", f"{JIBBLE_WS}/Projects/{project_id}?$select=id,assignees")
        if st != 200:
            state["errors"][key] = f"re-read {st}: {str(cur)[:400]}"
            print(f"  FAIL re-reading {project_id} before assigning {person_id}: {st}", file=sys.stderr)
            return False
        units[:] = [a["unitId"] for a in cur.get("assignees") or []]
        if person_id in units or (not units and not restrict):
            state["errors"].pop(key, None)
            return True
        st, _, resp = j.patch(JIBBLE_WS, f"Projects/{project_id}",
                              {"assignees": [{"unitId": u, "projectId": project_id} for u in units + [person_id]]})
        if st in (200, 204):
            units.append(person_id)
            state["errors"].pop(key, None)
            return True
        state["errors"][key] = f"{st}: {str(resp)[:400]}"
        print(f"  FAIL assigning {person_id} to {project_id}: {st} {str(resp)[:300]}", file=sys.stderr)
        return False

    # Projects this script created are visible only to the admin and the people who tracked time on them;
    # pre-existing projects matched by name keep their own visibility.
    if j.person:
        for p in plan["projects"]:
            if p["jibbleId"] == jid("project", p["clockifyId"]):
                assign(p["jibbleId"], j.person, restrict=True)
    # People whose entries are already in need their projects too, e.g. after a project was restricted mid-import.
    for project_id, person_id in sorted({(e["projectId"], e["personId"]) for e in entries
                                         if e["projectId"] and f"in:{e['clockifyId']}" in state["done"]}):
        assign(project_id, person_id)

    print(f"time entries: {len(todo)} to go")
    for i, e in enumerate(todo, 1):
        cid = e["clockifyId"]
        if e["projectId"] and not assign(e["projectId"], e["personId"]):
            continue
        inn = {"id": jid("in", cid), "personId": e["personId"], "type": "In", "time": e["start"],
               "clientType": "Web", "platform": PLATFORM}
        if e["projectId"]:
            inn["projectId"] = e["projectId"]
        if e.get("activityId"):
            inn["activityId"] = e["activityId"]
        if e["note"]:
            inn["note"] = e["note"]
        out = {"id": jid("out", cid), "personId": e["personId"], "type": "Out", "time": e["end"],
               "clientType": "Web", "platform": PLATFORM}
        if create(f"in:{cid}", JIBBLE_TT, "TimeEntries", inn):
            create(f"out:{cid}", JIBBLE_TT, "TimeEntries", out)
        if i % 25 == 0:
            save()
            print(f"  {i}/{len(todo)}")
    save()

    # Archive projects archived in Clockify and activities from done tasks, once their entries are in.
    if not limit and not only_email:
        pending = {i for e in plan["entries"] if f"out:{e['clockifyId']}" not in state["done"]
                   for i in (e["projectId"], e.get("activityId")) if i}
        targets = [("Projects", "project", p) for p in plan["projects"] if p["archived"]] + \
                  [("Activities", "activity", a) for a in plan["activities"] if a["done"]]
        for entity, kind, x in targets:
            key = f"archive:{x['clockifyId']}"
            # Only archive what this script created (deterministic id), and only once all its entries are in.
            if x["jibbleId"] == jid(kind, x["clockifyId"]) and key not in state["done"] and x["jibbleId"] not in pending:
                st, _, resp = j.patch(JIBBLE_WS, f"{entity}/{x['jibbleId']}", {"status": "Archived"})
                if st in (200, 204):
                    state["done"][key] = x["jibbleId"]
                    state["errors"].pop(key, None)
                else:
                    state["errors"][key] = f"{st}: {str(resp)[:400]}"
        save()
    ids = {x["clockifyId"] for k in ("clients", "projects", "activities", "people", "entries") for x in plan[k]}
    current = sum(1 for k in state["errors"] if k.startswith("assign:") or k.split(":", 1)[1] in ids)
    stale = len(state["errors"]) - current
    print(f"done: {len(state['done'])} objects, errors: {current}" +
          (f" (+{stale} for items no longer in the plan)" if stale else "") + f" (see {STATE})")


if __name__ == "__main__":
    load_env()
    a = sys.argv[1:]
    if not a or a[0] not in ("export", "plan", "apply"):
        sys.exit(__doc__)
    if a[0] == "export":
        cmd_export()
    elif a[0] == "plan":
        cmd_plan()
    else:
        lim = int(a[a.index("--limit") + 1]) if "--limit" in a else None
        only = a[a.index("--only-person") + 1] if "--only-person" in a else None
        cmd_apply(lim, only)
