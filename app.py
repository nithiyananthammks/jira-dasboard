import os
import re
from concurrent.futures import ThreadPoolExecutor
import requests
from requests.auth import HTTPBasicAuth
from flask import Flask, render_template, jsonify, request
from dotenv import load_dotenv

load_dotenv(override=True)

app = Flask(__name__)

JIRA_URL = os.environ.get("JIRA_URL", "https://hbuco.atlassian.net")
JIRA_USERNAME = os.environ.get("JIRA_USERNAME")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN")
AUTH = HTTPBasicAuth(JIRA_USERNAME, JIRA_API_TOKEN)
HEADERS = {"Accept": "application/json"}

# Persistent session for connection pooling
SESSION = requests.Session()
SESSION.auth = AUTH
SESSION.headers.update(HEADERS)

# Cache for user lookups (name -> (account_id, display_name))
_user_cache = {}

FIELDS = ["summary", "status", "assignee", "issuetype", "priority",
          "labels", "created", "updated", "customfield_10016", "customfield_10004",
          "customfield_10006", "parent", "timeoriginalestimate"]

TEAM_MEMBERS = {
    "Dev": ["Narayanan", "Ashwin", "Vignesh Murugan", "Manikandan", "Vikram",
            "Sabarisan", "Rajashri", "Geetha", "Srilatha", "Naveen"],
    "QA": ["Nithiyanantham", "Kalaivani", "Renishma", "Srimathi", "Akshaya",
           "Gayathri", "Arun", "Jamuna", "Sathish Kumar", "Soorya", "Suganya"],
    "DA": ["Raghuvaran", "Manikanta", "Reddamma", "Nareen"],
}

PROJECT_TEAMS = {
    "PURPLE": {"keys": ["PURPLE"], "Dev": ["Narayanan", "Ashwin", "Naveen"], "QA": ["Nithiyanantham", "Kalaivani"]},
    "OTECH": {"keys": ["OTECH"], "Dev": ["Srilatha"], "QA": ["Arun"]},
    "Openbeds O1": {"keys": ["ORANGE"], "Dev": ["Rajashri", "Geetha"], "QA": ["Srimathi", "Gayathri"]},
    "Openbeds O2": {"keys": ["ORANGE"], "Dev": ["Sabarisan"], "QA": ["Akshaya"]},
    "CareCo": {"keys": ["CARECO"], "Dev": ["Vignesh Murugan", "Manikandan", "Vikram"], "QA": ["Renishma"]},
    "DISTCH Automation": {"keys": ["QA"], "QA": ["Sathish Kumar", "Soorya"]},
    "DISTCH": {"keys": ["DISTCH"], "QA": ["Jamuna", "Suganya"]},
    "DISTCH DA": {"keys": ["DISTCH"], "Dev": ["Raghuvaran", "Manikanta"]},
    "CS DA": {"keys": ["DSPROD"], "Dev": ["Reddamma", "Nareen"]},
    "ERvive": {"keys": ["RED"], "QA": ["Arun"]},
}


def jira_search(jql, max_results=100):
    all_issues, next_token = [], None
    while True:
        body = {"jql": jql, "fields": FIELDS, "maxResults": max_results}
        if next_token:
            body["nextPageToken"] = next_token
        resp = SESSION.post(
            f"{JIRA_URL}/rest/api/3/search/jql",
            headers={"Content-Type": "application/json"},
            json=body)
        resp.raise_for_status()
        data = resp.json()
        all_issues.extend(data.get("issues", []))
        if data.get("isLast", True):
            break
        next_token = data.get("nextPageToken")
        if not next_token:
            break
    return all_issues


def extract_ticket(issue):
    f = issue["fields"]
    sp = f.get("customfield_10016") or f.get("customfield_10004")
    sprint_field = f.get("customfield_10006")
    sprint_info = None
    if sprint_field:
        if isinstance(sprint_field, dict):
            sprint_field = sprint_field.get("value", sprint_field)
        if isinstance(sprint_field, list) and sprint_field:
            s = sprint_field[-1]
            sprint_info = {
                "name": s.get("name"),
                "state": s.get("state"),
                "startDate": (s.get("startDate") or "")[:10],
                "endDate": (s.get("endDate") or "")[:10],
            }
    parent = f.get("parent")
    parent_info = None
    if parent:
        pf = parent.get("fields", {})
        parent_info = {
            "key": parent.get("key", ""),
            "summary": pf.get("summary", ""),
            "status": pf.get("status", {}).get("name", "") if pf.get("status") else "",
            "type": pf.get("issuetype", {}).get("name", "") if pf.get("issuetype") else "",
            "storyPoints": None,
            "_spField": pf.get("customfield_10016") or pf.get("customfield_10004"),
        }
    return {
        "key": issue["key"],
        "summary": f.get("summary", ""),
        "status": (f.get("status") or {}).get("name", "Unknown"),
        "type": (f.get("issuetype") or {}).get("name", ""),
        "priority": (f.get("priority") or {}).get("name", ""),
        "storyPoints": sp,
        "created": (f.get("created") or "")[:10],
        "updated": (f.get("updated") or "")[:10],
        "sprint": sprint_info,
        "parent": parent_info,
        "roleSP": None,
        "bugs": [],
    }


def get_role(display_name):
    """Determine role (Dev/QA) by matching display name against TEAM_MEMBERS."""
    dn = (display_name or "").lower()
    for role, members in TEAM_MEMBERS.items():
        for m in members:
            if m.lower() in dn or dn in m.lower():
                return "Dev" if role == "DA" else role
    return None


def _lookup_sp(args):
    """Lookup role SP from description then comments for a single issue."""
    key, role_label = args
    sp = _extract_sp_from_description(key, role_label)
    if not sp:
        sp = _extract_sp_from_comments(key, role_label)
    return key, sp


# Devs whose Dev SP = 60% of the story points field (SP 2 → 1)
SP_FROM_FIELD_DEVS = {"vignesh murugan", "manikandan", "vikram", "srilatha",
                      "raghuvaran", "manikanta", "reddamma", "nareen"}


def _dev_sp_from_field(sp):
    """Calculate Dev SP as 60% of story points, rounded (4.5→4, above 4.5→5)."""
    if sp is None or sp == 0:
        return None
    sp = float(sp)
    if sp <= 1:
        return 1.0
    if sp == 2:
        return 1.0
    if sp == 3:
        return 2.0
    import math
    result = sp * 0.6
    if result - math.floor(result) > 0.5:
        return float(math.ceil(result))
    return float(math.floor(result))


def _estimate_to_days(seconds):
    """Convert original estimate (seconds) to days, rounded up. 1d = 8h."""
    if not seconds:
        return None
    import math
    return float(math.ceil(seconds / (8 * 3600)))


def resolve_role_sp(tickets, role, display_name=None):
    """Batch resolve Dev or QA SP with parallel API calls."""
    role_label = role or "QA"
    is_dev = role_label == "Dev"

    if is_dev:
        dn = (display_name or "").lower()
        use_field = any(name in dn or dn in name for name in SP_FROM_FIELD_DEVS)

        if use_field:
            is_srilatha = "srilatha" in dn

            if is_srilatha:
                # Srilatha: check Dev Coding subtask SP first, then original estimate
                keys = [t["key"] for t in tickets]
                keys_jql = ",".join(f'"{k}"' for k in keys)
                sp_by_parent = {}
                est_by_parent = {}
                try:
                    subs = jira_search(
                        f'summary ~ "Dev Coding" AND parent in ({keys_jql})',
                        max_results=50)
                    for s in subs:
                        sf = s["fields"]
                        p = sf.get("parent")
                        pk = p.get("key") if p else None
                        if not pk:
                            continue
                        sp_val = sf.get("customfield_10016") or sf.get("customfield_10004")
                        if sp_val:
                            sp_by_parent[pk] = sp_val
                        est_val = sf.get("timeoriginalestimate")
                        if est_val:
                            est_by_parent[pk] = est_val
                except Exception:
                    pass
                for t in tickets:
                    k = t["key"]
                    if sp_by_parent.get(k):
                        t["roleSP"] = _dev_sp_from_field(sp_by_parent[k])
                    elif est_by_parent.get(k):
                        t["roleSP"] = _estimate_to_days(est_by_parent[k])
                    elif t["storyPoints"]:
                        t["roleSP"] = _dev_sp_from_field(t["storyPoints"])
                    if t["parent"]:
                        t["parent"].pop("_spField", None)
            else:
                # Other field devs: use assigned ticket SP directly
                for t in tickets:
                    t["roleSP"] = _dev_sp_from_field(t["storyPoints"])
                    if t["parent"]:
                        t["parent"].pop("_spField", None)
            return

        # Other devs: extract from assigned ticket description/comments
        all_keys = [(t["key"], role_label) for t in tickets]
        results = {}
        if all_keys:
            with ThreadPoolExecutor(max_workers=10) as pool:
                for key, sp in pool.map(_lookup_sp, all_keys):
                    results[key] = sp
        for t in tickets:
            t["roleSP"] = results.get(t["key"])
            if t["parent"]:
                t["parent"].pop("_spField", None)
        return

    # QA: existing behavior — check fields, then ticket, then parent
    needs_ticket_lookup = []
    needs_parent_lookup = set()
    parent_field_cache = {}

    for t in tickets:
        p = t["parent"]
        pk = p["key"] if p else None
        if t["storyPoints"]:
            t["roleSP"] = float(t["storyPoints"])
            if p:
                p["storyPoints"] = t["roleSP"]
        elif p and p.get("_spField"):
            val = float(p["_spField"])
            t["roleSP"] = val
            p["storyPoints"] = val
            parent_field_cache[pk] = val
        else:
            needs_ticket_lookup.append(t)
            if pk and pk not in parent_field_cache:
                needs_parent_lookup.add(pk)

    all_keys = [(t["key"], role_label) for t in needs_ticket_lookup] + [(pk, role_label) for pk in needs_parent_lookup]
    results = {}
    if all_keys:
        with ThreadPoolExecutor(max_workers=10) as pool:
            for key, sp in pool.map(_lookup_sp, all_keys):
                results[key] = sp

    parent_cache = {**parent_field_cache}
    for pk in needs_parent_lookup:
        parent_cache[pk] = results.get(pk)

    for t in needs_ticket_lookup:
        p = t["parent"]
        pk = p["key"] if p else None
        sp = results.get(t["key"])
        if not sp and pk:
            sp = parent_cache.get(pk)
        t["roleSP"] = sp
        if p:
            p["storyPoints"] = sp

    for t in tickets:
        if t["parent"]:
            t["parent"].pop("_spField", None)


def resolve_bugs(tickets, account_id, role=None):
    """Find bug tickets. Dev: subtasks of assigned ticket. QA: under parent ticket."""
    if role == "Dev":
        ticket_keys = set(t["key"] for t in tickets)
        if not ticket_keys:
            return
        keys_jql = ",".join(f'"{k}"' for k in ticket_keys)
        jql = (f'issuetype in (Bug, Bug-Subtask) AND parent in ({keys_jql}) '
               f'ORDER BY created DESC')
        try:
            bug_issues = jira_search(jql)
        except Exception:
            bug_issues = []
        bugs_by_ticket = {}
        for b in bug_issues:
            bf = b["fields"]
            summary = bf.get("summary", "")
            bug_status = (bf.get("status") or {}).get("name", "")
            if "qa time" in summary.lower():
                continue
            if "rejected" in bug_status.lower():
                continue
            p = bf.get("parent")
            pk = p.get("key") if p else None
            if pk and pk in ticket_keys:
                bugs_by_ticket.setdefault(pk, []).append({
                    "key": b["key"],
                    "summary": bf.get("summary", ""),
                    "status": (bf.get("status") or {}).get("name", ""),
                })
        for t in tickets:
            if t["key"] in bugs_by_ticket:
                t["bugs"] = bugs_by_ticket[t["key"]]
        return

    # QA: existing behavior — bugs under parent ticket
    parent_keys = set()
    for t in tickets:
        if t["parent"]:
            parent_keys.add(t["parent"]["key"])
    if not parent_keys:
        return
    parents_jql = ",".join(f'"{k}"' for k in parent_keys)
    jql = (f'issuetype in (Bug, Bug-Subtask) AND creator = "{account_id}" AND '
           f'(parent in ({parents_jql}) OR "Epic Link" in ({parents_jql})) '
           f'ORDER BY created DESC')
    try:
        bug_issues = jira_search(jql)
    except Exception:
        try:
            jql = (f'issuetype in (Bug, Bug-Subtask, Sub-task) AND '
                   f'creator = "{account_id}" AND parent in ({parents_jql}) '
                   f'ORDER BY created DESC')
            bug_issues = jira_search(jql)
        except Exception:
            bug_issues = []
    bugs_by_parent = {}
    for b in bug_issues:
        bf = b["fields"]
        bug_status = (bf.get("status") or {}).get("name", "")
        if "rejected" in bug_status.lower():
            continue
        p = bf.get("parent")
        pk = p.get("key") if p else None
        if pk and pk in parent_keys:
            bugs_by_parent.setdefault(pk, []).append({
                "key": b["key"],
                "summary": bf.get("summary", ""),
                "status": (bf.get("status") or {}).get("name", ""),
            })
    for t in tickets:
        if t["parent"] and t["parent"]["key"] in bugs_by_parent:
            t["bugs"] = bugs_by_parent[t["parent"]["key"]]


# Aliases for names that don't resolve correctly via Jira search
NAME_ALIASES = {
    "srilatha": "Srilatha Kommidi",
    "naveen": "Naveen Kumar Ambulapodi",
    "gayathri": "Gayathri Priya",
    "ashwin": "ag@bamboohealth.com",
    "vikram": "Vikram J A",
    "nithiyanantham": "Nithiyanantham Loganathan",
    "raghuvaran": "Raghuvaran Guduri",
    "manikanta": "Manikanta Visarapu",
    "reddamma": "Reddamma S.G",
    "nareen": "Nareen Patnana",
}

# Extra JQL filters per user (applied after base query)
USER_JQL_EXCLUDE = {
    "srilatha kommidi": 'AND summary !~ "Dev Coding"',
}


def find_user(name):
    # Check aliases first
    alias = NAME_ALIASES.get(name.lower())
    if alias:
        name = alias
    # Check cache
    cache_key = name.lower()
    if cache_key in _user_cache:
        return _user_cache[cache_key]
    # Try user search API first
    try:
        resp = SESSION.get(
            f"{JIRA_URL}/rest/api/3/user/search",
            params={"query": name, "maxResults": 5})
        resp.raise_for_status()
        users = resp.json()
        if users:
            result = (users[0]["accountId"], users[0].get("displayName", name))
            _user_cache[cache_key] = result
            return result
    except Exception:
        pass
    # Fallback: search recent issues and match assignee display name
    try:
        body = {"jql": "assignee is not EMPTY ORDER BY updated DESC",
                "fields": ["assignee"], "maxResults": 100}
        r = SESSION.post(f"{JIRA_URL}/rest/api/3/search/jql",
                         headers={"Content-Type": "application/json"},
                         json=body)
        r.raise_for_status()
        nl = name.lower()
        for issue in r.json().get("issues", []):
            a = issue["fields"].get("assignee") or {}
            dn = (a.get("displayName") or "").lower()
            email = (a.get("emailAddress") or "").lower()
            if nl in dn or dn in nl or nl == email or nl in email:
                result = (a.get("accountId"), a.get("displayName", name))
                _user_cache[cache_key] = result
                return result
    except Exception:
        pass
    return None, None


def parse_query(text):
    """Parse natural language into assignee name and optional sprint."""
    text = text.strip()
    sprint = None
    # Match "sprint <name>" explicitly
    sprint_match = re.search(
        r'(?:for\s+)?sprint\s+([\w][\w\-]*(?:[\s\-][\w][\w\-]*)*)', text, re.IGNORECASE)
    if sprint_match:
        sprint = sprint_match.group(1).strip()
        text = text[:sprint_match.start()] + text[sprint_match.end():]
    else:
        # Auto-detect sprint-like patterns: PROJ-2026-8 etc.
        sp = re.search(r'(?:for\s+)?([A-Z][A-Z0-9]*\-\d{4}\-\d+)', text)
        if sp:
            sprint = sp.group(1)
            text = text[:sp.start()] + text[sp.end():]
    # Clean up filler words
    filler = ["fetch", "get", "show", "details", "of", "for", "me",
              "the", "can", "you", "u", "please", "pls", "from",
              "what", "is", "are", "all", "tasks", "tickets", "work",
              "assigned", "to", "in", "about", "give", "bring"]
    for word in filler:
        text = re.sub(rf'\b{word}\b', '', text, flags=re.IGNORECASE)
    name = re.sub(r'\s+', ' ', text).strip().strip('?.,!')
    if not name and sprint:
        return None, None
    return name, sprint


def extract_text_from_adf(node):
    """Recursively extract plain text from Atlassian Document Format."""
    text = ""
    if isinstance(node, dict):
        if node.get("type") == "text":
            text += node.get("text", "")
        for c in node.get("content", []):
            text += extract_text_from_adf(c)
    elif isinstance(node, list):
        for c in node:
            text += extract_text_from_adf(c)
    return text


def _extract_sp_from_description(issue_key, role_label):
    """Fetch issue description and look for role SP (Dev/QA) under Effort section."""
    try:
        resp = SESSION.get(
            f"{JIRA_URL}/rest/api/3/issue/{issue_key}",
            params={"fields": "description"})
        resp.raise_for_status()
        desc = resp.json().get("fields", {}).get("description")
        if not desc:
            return None
        text = extract_text_from_adf(desc)
        pattern = rf'Effort.*?{role_label}(?:\s*Story\s*points)?\s*:\s*(\d+(?:\.\d+)?)'
        m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


def _extract_sp_from_comments(issue_key, role_label):
    """Fetch comments for an issue and extract role SP (Dev/QA) value."""
    try:
        resp = SESSION.get(
            f"{JIRA_URL}/rest/api/3/issue/{issue_key}/comment",
            params={"maxResults": 100})
        resp.raise_for_status()
        comments = resp.json().get("comments", [])
        for comment in reversed(comments):
            body = comment.get("body", {})
            text = ""
            for block in body.get("content", []):
                for item in block.get("content", []):
                    if item.get("type") == "text":
                        text += item.get("text", "")
            pattern = rf'{role_label}(?:\s*Story\s*points)?\s*:\s*(\d+(?:\.\d+)?)'
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return float(match.group(1))
    except Exception:
        pass
    return None


def extract_qa_sp_from_description(issue_key):
    return _extract_sp_from_description(issue_key, "QA")


def extract_qa_sp_from_comments(issue_key):
    return _extract_sp_from_comments(issue_key, "QA")


def extract_dev_sp_from_description(issue_key):
    return _extract_sp_from_description(issue_key, "Dev")


def extract_dev_sp_from_comments(issue_key):
    return _extract_sp_from_comments(issue_key, "Dev")





@app.route("/")
def index():
    return render_template("index.html", jira_url=JIRA_URL, team_members=TEAM_MEMBERS, project_teams=PROJECT_TEAMS)


@app.route("/api/sprints")
def sprints():
    name = request.args.get("name", "").strip()
    project = request.args.get("project", "").strip()

    # Collect account IDs to search
    account_ids = []
    proj_keys = []
    if project and project in PROJECT_TEAMS:
        team = PROJECT_TEAMS[project]
        proj_keys = team.get("keys", [])
        for role_key, role_members in team.items():
            if role_key == "keys" or not isinstance(role_members, list):
                continue
            for member in role_members:
                try:
                    aid, _ = find_user(member)
                    if aid:
                        account_ids.append(aid)
                except Exception:
                    pass
    elif name:
        alias = NAME_ALIASES.get(name.lower())
        if alias:
            name = alias
        try:
            aid, _ = find_user(name)
            if aid:
                account_ids.append(aid)
        except Exception:
            pass
        # Find project keys for this member
        for pname, team in PROJECT_TEAMS.items():
            for role_key, role_members in team.items():
                if role_key == "keys" or not isinstance(role_members, list):
                    continue
                if any(m.lower() in name.lower() or name.lower() in m.lower() for m in role_members):
                    proj_keys.extend(team.get("keys", []))

    if not account_ids:
        return jsonify({"sprints": []})

    proj_keys = list(set(proj_keys))
    seen = {}
    current_year = str(__import__("datetime").date.today().year)
    for account_id in account_ids:
        try:
            proj_filter = f' AND project in ({",".join(proj_keys)})' if proj_keys else ''
            issues = jira_search(
                f'assignee = "{account_id}" AND sprint is not EMPTY'
                f'{proj_filter} ORDER BY updated DESC', max_results=100)
        except Exception:
            continue
        for i in issues:
            sf = i["fields"].get("customfield_10006")
            if sf and isinstance(sf, dict):
                sf = sf.get("value", sf)
            if sf and isinstance(sf, list):
                for s in sf:
                    sn = s.get("name")
                    end = (s.get("endDate") or "")[:4]
                    if sn and sn not in seen and end >= current_year:
                        seen[sn] = s.get("state", "")
    sprint_list = [{"name": n, "state": st} for n, st in seen.items()]
    sprint_list.sort(key=lambda x: (x["state"] != "active", x["name"]), reverse=True)
    return jsonify({"sprints": sprint_list})


@app.route("/api/query")
def query():
    raw = request.args.get("q", "").strip()
    if not raw:
        return jsonify({"error": "Please type a query"}), 400

    name, sprint = parse_query(raw)
    # Explicit sprint param takes priority over parsed one
    explicit_sprint = request.args.get("sprint", "").strip()
    if explicit_sprint:
        sprint = explicit_sprint
    if not name:
        return jsonify({"error": "Couldn't identify an assignee name from your query"}), 400

    try:
        account_id, display_name = find_user(name)
    except Exception as e:
        return jsonify({"error": f"User lookup failed: {e}"}), 500
    if not account_id:
        return jsonify({"error": f"No user found matching '{name}'"}), 404

    quarter = request.args.get("quarter", "").strip()
    month = request.args.get("month", "").strip()

    QUARTER_DATES = {
        "Q1": ("01-01", "03-31"), "Q2": ("04-01", "06-30"),
        "Q3": ("07-01", "09-30"), "Q4": ("10-01", "12-31"),
    }

    jql = f'assignee = "{account_id}"'
    distch_members = [m.lower() for m in PROJECT_TEAMS.get("DISTCH Automation", {}).get("QA", [])]
    is_distch = any(m in display_name.lower() for m in distch_members)
    if is_distch:
        year = __import__("datetime").date.today().year
        jql += f' AND created >= "{year}-01-01"'
    if sprint:
        jql += f' AND sprint = "{sprint}"'
    elif month and month.isdigit():
        import calendar
        year = __import__("datetime").date.today().year
        m = int(month)
        last_day = calendar.monthrange(year, m)[1]
        jql += f' AND updated >= "{year}-{m:02d}-01" AND updated <= "{year}-{m:02d}-{last_day}"'
    elif quarter and quarter in QUARTER_DATES:
        year = __import__("datetime").date.today().year
        start, end = QUARTER_DATES[quarter]
        jql += f' AND updated >= "{year}-{start}" AND updated <= "{year}-{end}"'
    else:
        jql += ' AND updated >= startOfYear()'
    jql += ' ' + USER_JQL_EXCLUDE.get(display_name.lower(), '')
    jql += ' ORDER BY updated DESC'

    try:
        issues = jira_search(jql)
    except requests.exceptions.HTTPError as e:
        detail = e.response.text if e.response else str(e)
        return jsonify({"error": f"Jira search failed: {detail}"}), 500

    tickets = [extract_ticket(i) for i in issues]
    role = get_role(display_name)

    # QA: if parent ticket is assigned to QA member, hide QA Time subtasks
    # and extract SP from parent's description/comments only
    if role == "QA":
        parent_keys = set(t["key"] for t in tickets if not t["type"].lower().startswith("sub"))
        if parent_keys:
            # Filter out QA Time subtasks whose parent is already in the list
            tickets = [t for t in tickets
                       if not ("qa time" in t["summary"].lower()
                               and t["parent"] and t["parent"]["key"] in parent_keys)]
            # For parent tickets, clear SP field so it falls through to desc/comment extraction
            for t in tickets:
                if t["key"] in parent_keys:
                    t["storyPoints"] = None
                    if t["parent"]:
                        t["parent"]["_spField"] = None

    resolve_role_sp(tickets, role, display_name)
    resolve_bugs(tickets, account_id, role)

    # Dev: remove Bug/Bug-Subtask only if its parent is already in the ticket list
    if role == "Dev":
        assigned_keys = set(t["key"] for t in tickets)
        tickets = [t for t in tickets
                   if t["type"].lower() not in ("bug-subtask", "bug")
                   or not (t["parent"] and t["parent"]["key"] in assigned_keys)]

    # QA Automation tickets: hide parent and bugs
    for t in tickets:
        if "qa automation" in t["summary"].lower():
            t["parent"] = None
            t["bugs"] = []

    total_sp = sum(t["storyPoints"] or 0 for t in tickets)

    by_status = {}
    for t in tickets:
        by_status.setdefault(t["status"], []).append(t)

    sprints, prev_sprints, no_sprint = {}, {}, []
    current_year = str(__import__("datetime").date.today().year)
    for t in tickets:
        if t["sprint"]:
            # Skip sprints that ended before current year
            end = (t["sprint"].get("endDate") or "")[:4]
            if end and end < current_year:
                no_sprint.append(t)
                continue
            sname = t["sprint"]["name"]
            state = t["sprint"].get("state", "")
            target = sprints if (state == "active" or sprint) else prev_sprints
            target.setdefault(sname, {"info": t["sprint"], "tickets": [],
                                      "totalSP": 0, "totalRoleSP": 0})
            target[sname]["tickets"].append(t)
            target[sname]["totalSP"] += t["storyPoints"] or 0
            if t["roleSP"]:
                target[sname]["totalRoleSP"] += t["roleSP"]
        else:
            no_sprint.append(t)

    # Parent status breakdown (deduplicate by parent key)
    parent_status_counts = {}
    seen_parents = set()
    for t in tickets:
        if t["parent"] and t["parent"]["key"] not in seen_parents:
            seen_parents.add(t["parent"]["key"])
            ps = t["parent"]["status"] or "Unknown"
            parent_status_counts[ps] = parent_status_counts.get(ps, 0) + 1

    total_role_sp = sum(t["roleSP"] for t in tickets if t["roleSP"])
    # Count unique bugs across all tickets
    seen_bugs = set()
    for t in tickets:
        for b in t.get("bugs", []):
            seen_bugs.add(b["key"])
    total_bugs = len(seen_bugs)

    # Check if member is in DISTCH Automation
    if is_distch and tickets:
        def _get_counts(ticket):
            ticket["newTestCase"] = 0
            ticket["regressionFixes"] = 0
            try:
                resp = SESSION.get(
                    f"{JIRA_URL}/rest/api/3/issue/{ticket['key']}/comment",
                    params={"maxResults": 100})
                resp.raise_for_status()
                for c in resp.json().get("comments", []):
                    body = ""
                    cb = c.get("body")
                    if isinstance(cb, str):
                        body = cb
                    elif isinstance(cb, dict):
                        for block in cb.get("content", []):
                            for inline in block.get("content", []):
                                if inline.get("type") == "text":
                                    body += inline.get("text", "")
                    tc = re.search(r'[Nn]ew\s+[Tt]est\s*[Cc]ase\s*:\s*(\d+)', body)
                    rf = re.search(r'[Rr]egression\s+[Ff]ixes\s*:\s*(\d+)', body)
                    if tc:
                        ticket["newTestCase"] += int(tc.group(1))
                    if rf:
                        ticket["regressionFixes"] += int(rf.group(1))
            except Exception:
                pass
        with ThreadPoolExecutor(max_workers=10) as pool:
            pool.map(_get_counts, tickets)

    resp = {
        "assignee": display_name,
        "role": role,
        "isDistchAutomation": is_distch,
        "parsedQuery": {"name": name, "sprint": sprint, "quarter": quarter, "month": month},
        "totalTickets": len(tickets),
        "totalStoryPoints": total_sp,
        "totalRoleSP": total_role_sp,
        "totalBugs": total_bugs,
        "byStatus": {s: len(ts) for s, ts in by_status.items()},
        "parentStatusCounts": parent_status_counts,
        "tickets": tickets,
        "currentSprints": sprints,
        "previousSprints": prev_sprints,
        "backlog": [] if is_distch else no_sprint,
    }
    if is_distch:
        resp["totalNewTestCases"] = sum(t.get("newTestCase", 0) for t in tickets)
        resp["totalRegressionFixes"] = sum(t.get("regressionFixes", 0) for t in tickets)
    return jsonify(resp)


def _sprint_summary(account_id, display_name, sprint_name, role):
    """Fetch and summarize a single sprint for comparison."""
    jql = (f'assignee = "{account_id}" AND sprint = "{sprint_name}"'
           f' {USER_JQL_EXCLUDE.get(display_name.lower(), "")}'
           f' ORDER BY updated DESC')
    issues = jira_search(jql)
    tickets = [extract_ticket(i) for i in issues]
    if role == "QA":
        parent_keys = set(t["key"] for t in tickets if not t["type"].lower().startswith("sub"))
        if parent_keys:
            tickets = [t for t in tickets
                       if not ("qa time" in t["summary"].lower()
                               and t["parent"] and t["parent"]["key"] in parent_keys)]
            for t in tickets:
                if t["key"] in parent_keys:
                    t["storyPoints"] = None
                    if t["parent"]:
                        t["parent"]["_spField"] = None
    resolve_role_sp(tickets, role, display_name)
    resolve_bugs(tickets, account_id, role)
    if role == "Dev":
        assigned_keys = set(t["key"] for t in tickets)
        tickets = [t for t in tickets
                   if t["type"].lower() not in ("bug-subtask", "bug")
                   or not (t["parent"] and t["parent"]["key"] in assigned_keys)]
    for t in tickets:
        if "qa automation" in t["summary"].lower():
            t["parent"] = None
            t["bugs"] = []
    by_status = {}
    for t in tickets:
        by_status.setdefault(t["status"], []).append(t)
    seen_bugs = set()
    for t in tickets:
        for b in t.get("bugs", []):
            seen_bugs.add(b["key"])
    return {
        "sprint": sprint_name,
        "totalTickets": len(tickets),
        "totalSP": sum(t["storyPoints"] or 0 for t in tickets),
        "totalRoleSP": sum(t["roleSP"] for t in tickets if t["roleSP"]),
        "totalBugs": len(seen_bugs),
        "byStatus": {s: len(ts) for s, ts in by_status.items()},
        "tickets": tickets,
    }


@app.route("/api/compare")
def compare():
    name = request.args.get("name", "").strip()
    sprint1 = request.args.get("sprint1", "").strip()
    sprint2 = request.args.get("sprint2", "").strip()
    if not name or not sprint1 or not sprint2:
        return jsonify({"error": "name, sprint1, and sprint2 are required"}), 400
    alias = NAME_ALIASES.get(name.lower())
    if alias:
        name = alias
    try:
        account_id, display_name = find_user(name)
    except Exception as e:
        return jsonify({"error": f"User lookup failed: {e}"}), 500
    if not account_id:
        return jsonify({"error": f"No user found matching '{name}'"}), 404
    role = get_role(display_name)
    s1 = _sprint_summary(account_id, display_name, sprint1, role)
    s2 = _sprint_summary(account_id, display_name, sprint2, role)
    return jsonify({
        "assignee": display_name,
        "role": role,
        "sprint1": s1,
        "sprint2": s2,
    })


@app.route("/api/project/compare")
def project_compare():
    project = request.args.get("project", "").strip()
    q1 = request.args.get("q1", "").strip()
    q2 = request.args.get("q2", "").strip()
    if not project or project not in PROJECT_TEAMS:
        return jsonify({"error": f"Unknown project. Available: {list(PROJECT_TEAMS.keys())}"}), 400
    if not q1 or not q2:
        return jsonify({"error": "q1 and q2 (quarters) are required"}), 400

    team = PROJECT_TEAMS[project]
    project_keys = team.get("keys")

    def _quarter_data(quarter):
        members = []
        for role in ("Dev", "QA"):
            for name in team.get(role, []):
                data = _member_summary(name, project_keys=project_keys, project_name=project, quarter=quarter)
                if data:
                    members.append(data)
        return {
            "quarter": quarter,
            "totalTickets": sum(m["totalTickets"] for m in members),
            "totalDevSP": sum(m["totalRoleSP"] for m in members if m["role"] == "Dev"),
            "totalQASP": sum(m["totalRoleSP"] for m in members if m["role"] == "QA"),
            "totalBugsFixed": sum(m["totalBugs"] for m in members if m["role"] == "Dev"),
            "totalBugsIdentified": sum(m["totalBugs"] for m in members if m["role"] == "QA"),
            "members": members,
        }

    data1 = _quarter_data(q1)
    data2 = _quarter_data(q2)
    return jsonify({"project": project, "q1": data1, "q2": data2})


def _member_summary(name, sprint=None, project_keys=None, project_name=None, quarter=None):
    """Fetch summary for a single team member."""
    try:
        account_id, display_name = find_user(name)
    except Exception:
        return None
    if not account_id:
        return None
    role = get_role(display_name)
    jql = f'assignee = "{account_id}"'
    if project_keys:
        keys_str = ",".join(project_keys)
        jql += f' AND project in ({keys_str})'
    if sprint:
        jql += f' AND sprint = "{sprint}"'
    elif quarter:
        import calendar
        year = __import__("datetime").date.today().year
        QUARTER_DATES = {"Q1": ("01-01", "03-31"), "Q2": ("04-01", "06-30"),
                         "Q3": ("07-01", "09-30"), "Q4": ("10-01", "12-31")}
        if quarter in QUARTER_DATES:
            start, end = QUARTER_DATES[quarter]
            jql += f' AND updated >= "{year}-{start}" AND updated <= "{year}-{end}"'
    elif project_name == "DISTCH Automation":
        year = __import__("datetime").date.today().year
        jql += f' AND created >= "{year}-01-01"'
    else:
        jql += ' AND sprint in openSprints()'
    jql += ' ' + USER_JQL_EXCLUDE.get(display_name.lower(), '')
    jql += ' ORDER BY updated DESC'
    try:
        issues = jira_search(jql)
    except Exception:
        issues = []
    tickets = [extract_ticket(i) for i in issues]
    # QA parent ticket handling
    if role == "QA":
        parent_keys = set(t["key"] for t in tickets if not t["type"].lower().startswith("sub"))
        if parent_keys:
            tickets = [t for t in tickets
                       if not ("qa time" in t["summary"].lower()
                               and t["parent"] and t["parent"]["key"] in parent_keys)]
            for t in tickets:
                if t["key"] in parent_keys:
                    t["storyPoints"] = None
                    if t["parent"]:
                        t["parent"]["_spField"] = None
    resolve_role_sp(tickets, role, display_name)
    resolve_bugs(tickets, account_id, role)
    if role == "Dev":
        assigned_keys = set(t["key"] for t in tickets)
        tickets = [t for t in tickets
                   if t["type"].lower() not in ("bug-subtask", "bug")
                   or not (t["parent"] and t["parent"]["key"] in assigned_keys)]
    for t in tickets:
        if "qa automation" in t["summary"].lower():
            t["parent"] = None
            t["bugs"] = []
    seen_bugs = set()
    for t in tickets:
        for b in t.get("bugs", []):
            seen_bugs.add(b["key"])
    by_status = {}
    for t in tickets:
        by_status.setdefault(t["status"], []).append(t)

    # For DISTCH Automation, extract New Testcase / Regression Fixes from comments
    is_distch = project_name == "DISTCH Automation"
    if is_distch and tickets:
        def _get_counts(ticket):
            ticket["newTestCase"] = 0
            ticket["regressionFixes"] = 0
            try:
                resp = SESSION.get(
                    f"{JIRA_URL}/rest/api/3/issue/{ticket['key']}/comment",
                    params={"maxResults": 100})
                resp.raise_for_status()
                for c in resp.json().get("comments", []):
                    body = ""
                    cb = c.get("body")
                    if isinstance(cb, str):
                        body = cb
                    elif isinstance(cb, dict):
                        for block in cb.get("content", []):
                            for inline in block.get("content", []):
                                if inline.get("type") == "text":
                                    body += inline.get("text", "")
                    tc = re.search(r'[Nn]ew\s+[Tt]est\s*[Cc]ase\s*:\s*(\d+)', body)
                    rf = re.search(r'[Rr]egression\s+[Ff]ixes\s*:\s*(\d+)', body)
                    if tc:
                        ticket["newTestCase"] += int(tc.group(1))
                    if rf:
                        ticket["regressionFixes"] += int(rf.group(1))
            except Exception:
                pass
        with ThreadPoolExecutor(max_workers=10) as pool:
            pool.map(_get_counts, tickets)

    result = {
        "name": display_name,
        "role": role,
        "isDistchAutomation": is_distch,
        "totalTickets": len(tickets),
        "totalRoleSP": sum(t["roleSP"] for t in tickets if t["roleSP"]),
        "totalBugs": len(seen_bugs),
        "byStatus": {s: len(ts) for s, ts in by_status.items()},
        "tickets": tickets,
    }
    if is_distch:
        result["totalNewTestCases"] = sum(t.get("newTestCase", 0) for t in tickets)
        result["totalRegressionFixes"] = sum(t.get("regressionFixes", 0) for t in tickets)
    return result


@app.route("/api/project")
def project_view():
    project = request.args.get("project", "").strip()
    sprint = request.args.get("sprint", "").strip() or None
    quarter = request.args.get("quarter", "").strip() or None
    if not project or project not in PROJECT_TEAMS:
        return jsonify({"error": f"Unknown project. Available: {list(PROJECT_TEAMS.keys())}"}), 400
    team = PROJECT_TEAMS[project]
    project_keys = team.get("keys")
    members = []
    for role in ("Dev", "QA"):
        for name in team.get(role, []):
            data = _member_summary(name, sprint, project_keys, project_name=project, quarter=quarter)
            if data:
                members.append(data)
    # Totals
    is_distch = project == "DISTCH Automation"
    total_dev_sp = sum(m["totalRoleSP"] for m in members if m["role"] == "Dev")
    total_qa_sp = sum(m["totalRoleSP"] for m in members if m["role"] == "QA")
    total_tickets = sum(m["totalTickets"] for m in members)
    total_bugs_fixed = sum(m["totalBugs"] for m in members if m["role"] == "Dev")
    total_bugs_identified = sum(m["totalBugs"] for m in members if m["role"] == "QA")
    resp = {
        "project": project,
        "sprint": sprint,
        "quarter": quarter,
        "members": members,
        "isDistchAutomation": is_distch,
        "devOnly": not team.get("QA"),
        "totalDevSP": total_dev_sp,
        "totalQASP": total_qa_sp,
        "totalTickets": total_tickets,
        "totalBugsFixed": total_bugs_fixed,
        "totalBugsIdentified": total_bugs_identified,
    }
    if is_distch:
        resp["totalNewTestCases"] = sum(m.get("totalNewTestCases", 0) for m in members)
        resp["totalRegressionFixes"] = sum(m.get("totalRegressionFixes", 0) for m in members)
    return jsonify(resp)


AUTOMATION_PROJECTS = {
    "Controlled Substance": {"keys": ["PURPLE"], "search": "QA Automation", "teams": ["PURPLE"]},
    "CareCo": {"keys": ["CARECO"], "search": "Careco Test Automation", "teams": ["CareCo"]},
    "Openbeds": {"keys": ["ORANGE"], "search": "QA Automation", "teams": ["Openbeds O1", "Openbeds O2"]},
}


@app.route("/api/automation")
def automation_view():
    # Collect QA account IDs per automation project
    qa_ids = {}
    for label, cfg in AUTOMATION_PROJECTS.items():
        ids = {}
        for team_name in cfg.get("teams", []):
            for name in PROJECT_TEAMS.get(team_name, {}).get("QA", []):
                try:
                    aid, dn = find_user(name)
                    if aid:
                        ids[aid] = dn
                except Exception:
                    pass
        qa_ids[label] = ids

    results = {}
    year = __import__("datetime").date.today().year
    for label, cfg in AUTOMATION_PROJECTS.items():
        proj_keys = ",".join(cfg["keys"])
        summary_filter = f' AND summary ~ "{cfg["search"]}"' if cfg["search"] else ''
        jql = (f'(project in ({proj_keys}){summary_filter}'
               f' AND created >= "{year}-01-01") OR key = PURPLE-5601'
               f' ORDER BY updated DESC') if label == "Controlled Substance" else (
               f'(project in ({proj_keys}){summary_filter}'
               f' AND created >= "{year}-01-01") OR key = ORANGE-25686'
               f' ORDER BY updated DESC') if label == "Openbeds" else (
               f'project in ({proj_keys}){summary_filter}'
               f' AND created >= "{year}-01-01" ORDER BY updated DESC')
        try:
            issues = jira_search(jql)
        except Exception:
            issues = []
        allowed = qa_ids.get(label, {})
        filtered = []
        for i in issues:
            a = i["fields"].get("assignee") or {}
            aid = a.get("accountId", "")
            if aid in allowed:
                filtered.append({"key": i["key"],
                    "status": (i["fields"].get("status") or {}).get("name", ""),
                    "summary": i["fields"].get("summary", ""),
                    "assignee": allowed[aid],
                    "newTestCase": 0, "regressionFixes": 0})
        # Fetch comments in parallel to extract counts
        def _get_counts(ticket):
            try:
                resp = SESSION.get(
                    f"{JIRA_URL}/rest/api/3/issue/{ticket['key']}/comment",
                    params={"maxResults": 100})
                resp.raise_for_status()
                for c in resp.json().get("comments", []):
                    body = ""
                    cb = c.get("body")
                    if isinstance(cb, str):
                        body = cb
                    elif isinstance(cb, dict):
                        for block in cb.get("content", []):
                            for inline in block.get("content", []):
                                if inline.get("type") == "text":
                                    body += inline.get("text", "")
                    tc = re.search(r'[Nn]ew\s+[Tt]est\s*[Cc]ase\s*:\s*(\d+)', body)
                    rf = re.search(r'[Rr]egression\s+[Ff]ixes\s*:\s*(\d+)', body)
                    if tc:
                        ticket["newTestCase"] += int(tc.group(1))
                    if rf:
                        ticket["regressionFixes"] += int(rf.group(1))
            except Exception:
                pass
        with ThreadPoolExecutor(max_workers=10) as pool:
            pool.map(_get_counts, filtered)
        results[label] = filtered
    return jsonify(results)


@app.route("/api/defects")
def defects_view():
    team = request.args.get("team", "").strip()
    member = request.args.get("member", "").strip()
    if not team or team not in TEAM_MEMBERS:
        return jsonify({"error": f"team required. Options: {list(TEAM_MEMBERS.keys())}"}), 400
    if not member:
        return jsonify({"error": "member required"}), 400
    try:
        account_id, display_name = find_user(member)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if not account_id:
        return jsonify({"error": f"No user found for '{member}'"}), 404

    year = __import__("datetime").date.today().year
    role = "Dev" if team in ("Dev", "DA") else "QA"

    # Step 1: Get member's assigned tickets for the year (same as main dashboard)
    jql = (f'assignee = "{account_id}" AND updated >= startOfYear()'
           f' {USER_JQL_EXCLUDE.get(display_name.lower(), "")}'
           f' ORDER BY updated DESC')
    try:
        assigned = jira_search(jql)
    except Exception:
        assigned = []
    tickets = [extract_ticket(i) for i in assigned]

    # QA: filter same as dashboard
    if role == "QA":
        pk_set = set(t["key"] for t in tickets if not t["type"].lower().startswith("sub"))
        if pk_set:
            tickets = [t for t in tickets
                       if not ("qa time" in t["summary"].lower()
                               and t["parent"] and t["parent"]["key"] in pk_set)]

    # Step 2: Find bugs using same logic as resolve_bugs
    bug_issues = []
    if role == "Dev":
        ticket_keys = set(t["key"] for t in tickets)
        if ticket_keys:
            keys_jql = ",".join(f'"{k}"' for k in ticket_keys)
            try:
                bug_issues = jira_search(
                    f'issuetype in (Bug, Bug-Subtask) AND parent in ({keys_jql})'
                    f' ORDER BY created DESC')
            except Exception:
                bug_issues = []
    else:
        parent_keys = set()
        for t in tickets:
            if t["parent"]:
                parent_keys.add(t["parent"]["key"])
        if parent_keys:
            parents_jql = ",".join(f'"{k}"' for k in parent_keys)
            try:
                bug_issues = jira_search(
                    f'issuetype in (Bug, Bug-Subtask) AND creator = "{account_id}"'
                    f' AND (parent in ({parents_jql}) OR "Epic Link" in ({parents_jql}))'
                    f' ORDER BY created DESC')
            except Exception:
                try:
                    bug_issues = jira_search(
                        f'issuetype in (Bug, Bug-Subtask, Sub-task) AND'
                        f' creator = "{account_id}" AND parent in ({parents_jql})'
                        f' ORDER BY created DESC')
                except Exception:
                    bug_issues = []

        # Also include bugs in "Bugs to Discuss/Reject" sprint created by QA member
        try:
            discuss_bugs = jira_search(
                f'issuetype in (Bug, Bug-Subtask) AND creator = "{account_id}"'
                f' AND sprint = "Bugs to Discuss/Reject"'
                f' ORDER BY created DESC')
            bug_issues.extend(discuss_bugs)
        except Exception:
            pass

    # Step 3: Filter (same as resolve_bugs) and group by sprint
    sprints = {}
    no_sprint = 0
    seen_keys = set()
    for b in bug_issues:
        bf = b["fields"]
        bug_status = (bf.get("status") or {}).get("name", "")
        if "rejected" in bug_status.lower():
            continue
        if role == "Dev" and "qa time" in (bf.get("summary") or "").lower():
            continue
        if b["key"] in seen_keys:
            continue
        seen_keys.add(b["key"])
        sf = bf.get("customfield_10006")
        if sf and isinstance(sf, list) and sf:
            s = sf[-1]
            sname = s.get("name", "Unknown")
            end = (s.get("endDate") or "")[:4]
            if end and end < str(year):
                no_sprint += 1
                continue
            if sname not in sprints:
                sprints[sname] = {"name": sname, "state": s.get("state", ""),
                                  "startDate": (s.get("startDate") or "")[:10],
                                  "endDate": (s.get("endDate") or "")[:10], "count": 0}
            sprints[sname]["count"] += 1
        else:
            no_sprint += 1

    sprint_list = sorted(sprints.values(), key=lambda x: x.get("startDate", ""))
    col = "Bugs Identified" if team == "QA" else "Bugs Fixed"
    return jsonify({"member": display_name, "team": team, "column": col,
                    "sprints": sprint_list, "noSprint": no_sprint,
                    "total": sum(s["count"] for s in sprint_list) + no_sprint})


if __name__ == "__main__":
    if not JIRA_USERNAME or not JIRA_API_TOKEN:
        print("Set JIRA_USERNAME and JIRA_API_TOKEN environment variables")
        exit(1)
    app.run(debug=True, port=5050, host='0.0.0.0')
