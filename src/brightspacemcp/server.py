from typing import Any
import asyncio
import logging
import httpx2 as ht
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from brightspacemcp import auth as ba
from datetime import datetime, timedelta, timezone
import hmac
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
import os
import uvicorn


logger = logging.getLogger(__name__)

# Inbound MCP-client auth: the RequireToken middleware (see main()) rejects any
# request to /mcp/ without a matching `Authorization: Bearer <MCP_INBOUND_TOKEN>`
# header. The MCPServer itself is constructed with no auth/token_verifier.
mcp = MCPServer("brightspace")

# Served on 127.0.0.1 behind an nginx proxy that terminates TLS for
# https://<MCP_PUBLIC_HOST> and forwards the original Host header. The SDK
# auto-enables DNS-rebinding protection for loopback binds and would otherwise
# 421 that Host, so the public host has to be in allowed_hosts/_origins.
PUBLIC_HOST = os.environ.get("MCP_PUBLIC_HOST", "mcp.xennick.com")
TRANSPORT_SECURITY = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[PUBLIC_HOST, "127.0.0.1:8008", "localhost:8008"],
    allowed_origins=[f"https://{PUBLIC_HOST}"],
)

async def request(api_pull, params=None):
    cookies = ba.return_cookies()
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0'}
    async with ht.AsyncClient() as client:
        try:
            response = await client.get(
                f'https://purdue.brightspace.com/d2l/api{api_pull}',
                cookies=cookies,
                headers=headers,
                params=params,
                timeout=30.0,
            )
            response.raise_for_status()
            return response.json()
        except ht.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.warning("Brightspace %s -> HTTP %s", api_pull, status)
            # 401/403 almost always means the scraped session cookies expired.
            hint = "session cookies likely expired - refresh .env" if status in (401, 403) else None
            return {"error": f"HTTP {status}", "endpoint": api_pull, "hint": hint}
        except Exception as exc:
            logger.warning("Brightspace %s -> %s", api_pull, exc.__class__.__name__)
            return {"error": exc.__class__.__name__, "endpoint": api_pull}

@mcp.tool()
async def getUser():
    """
    Returns the user's information.
    """
    return await request('/lp/1.0/users/whoami')

@mcp.tool()
async def getClasses(full: bool | str = False):
    """
    Returns the user's course enrollments. Course names are the "Name" field.

    full=False (default): a Brightspace enrollments response filtered down to the
        current, accessible Course Offerings (skipping old terms and duplicate
        Group/Lab sub-orgs), de-duped by course code -> [{id, name, code}, ...].
    full=True: the raw enrollments feed, including past and hidden enrollments.

    Pass False most of the time to conserve tokens. Accepts a real bool or a
    stringified one ("true"/"false").
    """
    enrollments = await request('/lp/1.32/enrollments/myenrollments/')
    if _coerce_bool(full):
        return enrollments
    return await filter_current_courses(enrollments)

@mcp.tool()
async def getAssignedGrades(orgid):
    """
    Retrieves the assigned grades for a specific course.
    
    Input uses the OrgUnit ID found in the getClasses method.
    """
    return await request(f'/le/1.82/{orgid}/grades/values/myGradeValues/')

@mcp.tool()
async def getAllGrades(orgid):
    """
    Retrieves all grades for a specific course.
    
    Input uses the OrgUnit ID found in the getClasses method.
    """
    return await request(f'/le/1.82/{orgid}/grades/')

@mcp.tool()
async def getWeeklyTodo(orgid, days: int | str = 7, include_completed: bool | str = False):
    """
    Retrieves the calling user's scheduled content items (assignments, quizzes,
    readings, etc.) due within the next N days (default 7) for a specific course.

    Input uses the OrgUnit ID found in the getClasses method.
    Each item includes ItemName, DueDate, and ItemUrl (a direct link).

    By default, items that are already completed (populated CompletionDate /
    DateCompleted) are filtered out server-side; pass include_completed=True to
    get them back. For everything outstanding with no date window use
    getAllDueItems; to confirm submission/grading status cross-check
    getAssignedGrades.

    COVERAGE LIMIT: this feed only contains items linked into course content.
    A quiz built in the Quizzes tool, or a homework/quiz whose due date lives
    only on the course calendar, will NOT appear here - call getEverythingDue
    for the merged (content + quizzes + calendar) picture.
    """
    days = _coerce_days(days)
    include_completed = _coerce_bool(include_completed)
    now = datetime.now(timezone.utc)
    params = {
        "startDateTime": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "endDateTime": (now + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    payload = await request(f'/le/1.82/{orgid}/content/myItems/due/', params=params)
    return _filter_due_payload(payload, include_completed)

@mcp.tool()
async def getAllDueItems(orgid, include_completed: bool | str = False):
    """
    Retrieves the calling user's scheduled items for a specific course with NO
    date window (past-due, this week, and everything further out).

    Input uses the OrgUnit ID found in the getClasses method.

    By default, already-completed items (populated CompletionDate /
    DateCompleted) are filtered out server-side; pass include_completed=True for
    the full feed. Items also carry IsLocked / IsHidden / StartDate / EndDate
    availability fields where set - do not assume an item is available without
    checking those. Cross-check getAssignedGrades for submission status.

    COVERAGE LIMIT: this feed only contains items linked into course content.
    A quiz built in the Quizzes tool, or a homework/quiz whose due date lives
    only on the course calendar, will NOT appear here - call getEverythingDue
    for the merged (content + quizzes + calendar) picture.
    """
    include_completed = _coerce_bool(include_completed)
    payload = await request(f'/le/1.82/{orgid}/content/myItems/due/')
    return _filter_due_payload(payload, include_completed)

def _project_toc(toc):
    """
    Flatten a raw D2L /content/toc tree into a flat list of topic dicts,
    recursing nested Modules and recording the module path.

    The /content/toc payload carries NO due date and NO per-user completion
    state - only structure and availability. So this projection surfaces exactly
    what is there: StartDate/EndDate (from StartDateTime/EndDateTime), IsLocked,
    IsHidden, IsBroken, IsExempt, plus Type/Url/GradeItemId/LastModified. For
    due dates and completion status use getAllDueItems / getWeeklyTodo, whose
    items carry DueDate, StartDate, EndDate and DateCompleted.
    """
    if not isinstance(toc, dict):
        return toc
    out = []

    def walk(module, module_path):
        title = module.get("Title") or ""
        path = f"{module_path} / {title}" if module_path else title
        for topic in module.get("Topics", []) or []:
            out.append({
                "Module": path,
                "Title": topic.get("Title"),
                "Type": topic.get("TypeIdentifier"),
                "Url": topic.get("Url"),
                "TopicId": topic.get("TopicId"),
                "GradeItemId": topic.get("GradeItemId"),
                "StartDate": topic.get("StartDateTime"),
                "EndDate": topic.get("EndDateTime"),
                "IsLocked": topic.get("IsLocked"),
                "IsHidden": topic.get("IsHidden"),
                "IsBroken": topic.get("IsBroken"),
                "IsExempt": topic.get("IsExempt"),
                "LastModified": topic.get("LastModifiedDate"),
            })
        for child in module.get("Modules", []) or []:
            walk(child, path)

    for module in toc.get("Modules", []) or []:
        walk(module, "")
    return out


@mcp.tool()
async def getCourseToc(orgid, full: bool | str = False):
    """
    Retrieves the table of contents (modules and topics) for a specific course.
    Useful for browsing/searching all content (readings, videos, lectures), not
    just gradeable "due" items.

    Input uses the OrgUnit ID found in the getClasses method.

    full=False (default): a flat list of topics, one dict each - Module path,
        Title, Type, Url, TopicId, GradeItemId, StartDate, EndDate, IsLocked,
        IsHidden, IsBroken, IsExempt, LastModified. Much smaller and not nested.
        NOTE: the TOC feed has no due dates and no completion state; for those
        use getAllDueItems / getWeeklyTodo. Check IsLocked / IsHidden / StartDate
        / EndDate before calling an item "available".
    full=True: the raw, deeply-nested D2L tree (large; may spill to a file).

    COVERAGE LIMIT: the TOC only lists content that has been linked into a
    module, with no due dates. For a complete deadline picture (content +
    Quizzes tool + course calendar) use getEverythingDue.
    """
    raw = await request(f'/le/1.82/{orgid}/content/toc')
    if _coerce_bool(full):
        return raw
    return _project_toc(raw)

@mcp.tool()
async def getWeeklyCalendarEvents(orgid, days: int | str = 7):
    """
    Retrieves the calling user's calendar events for a specific course within
    the next N days (default 7). Includes due dates, availability windows,
    and reminders, each with a CalendarEventViewUrl link.

    Input uses the OrgUnit ID found in the getClasses method.
    `days` accepts an int or a numeric string and is clamped to 1..365.
    """
    days = _coerce_days(days)
    now = datetime.now(timezone.utc)
    params = {
        "startDateTime": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "endDateTime": (now + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    return await request(f'/le/1.82/{orgid}/calendar/events/myEvents/', params=params)


def _coerce_days(days, *, default=7, minimum=1, maximum=365):
    """
    Accept the N-day window as an int or a numeric string. Some MCP clients
    serialize scalar args as strings, which would otherwise fail integer
    schema validation. Clamps to a sane range; falls back to the default on
    anything unparseable.
    """
    try:
        days = int(days)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, days))


def _coerce_bool(value, *, default=False):
    """Accept a real bool or a stringified one ('true'/'1'/'yes')."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "y")
    return default


_COMPLETION_KEYS = ("CompletionDate", "DateCompleted", "CompletedDate")


def _is_completed(item):
    return isinstance(item, dict) and any(item.get(k) for k in _COMPLETION_KEYS)


def _filter_due_payload(payload, include_completed):
    """
    Drop already-completed entries from a /content/myItems/due/ response
    ({"Objects": [...], "Next": ...}) unless include_completed is set. Leaves
    anything that isn't that shape (e.g. None on request failure) untouched.
    """
    if include_completed or not isinstance(payload, dict):
        return payload
    objs = payload.get("Objects")
    if not isinstance(objs, list):
        return payload
    return {**payload, "Objects": [o for o in objs if not _is_completed(o)]}


def _filter_quiz_payload(payload, include_inactive):
    """
    Drop quizzes that are toggled off (IsActive is false) from a /quizzes/
    ObjectListPage response ({"Objects": [...], "Next": ...}) unless
    include_inactive is set. Leaves anything that isn't that shape untouched.
    """
    if include_inactive or not isinstance(payload, dict):
        return payload
    objs = payload.get("Objects")
    if not isinstance(objs, list):
        return payload
    return {
        **payload,
        "Objects": [
            o for o in objs
            if not isinstance(o, dict) or o.get("IsActive", True)
        ],
    }


def _norm_title_key(name):
    """Collapse whitespace / case so titles from different feeds can match."""
    return " ".join(str(name or "").split()).strip().lower()


# Calendar events whose AssociatedEntityType ends in one of these are structural
# markers (a module/week becoming available), not gradeable deliverables.
_CALENDAR_SKIP_ENTITY_SUFFIXES = ("ModuleCO",)

_CALENDAR_TYPE_LABELS = {
    "TopicCO": "Content topic",
    "Dropbox": "Assignment",
    "Quiz": "Quiz",
    "Discussion": "Discussion",
    "Checklist": "Checklist",
    "Survey": "Survey",
}


def _calendar_entity_type(event):
    ae = event.get("AssociatedEntity") or {}
    return ae.get("AssociatedEntityType") or ""


def _calendar_type_label(entity_type):
    tail = entity_type.rsplit(".", 1)[-1] if entity_type else ""
    return _CALENDAR_TYPE_LABELS.get(tail, tail or "Event")


def _project_calendar_payload(payload):
    """
    Reduce a /calendar/events/myEvents/ ObjectListPage to the gradeable
    deliverables it carries. Drops module/week "opens" markers (ModuleCO) and
    any event with no associated entity. Calendar events carry NO completion
    or submission state, so entries sourced only from here cannot be filtered
    by include_completed.
    """
    if not isinstance(payload, dict):
        return []
    out = []
    for e in payload.get("Objects") or []:
        if not isinstance(e, dict):
            continue
        entity_type = _calendar_entity_type(e)
        if not entity_type:
            continue
        if entity_type.endswith(_CALENDAR_SKIP_ENTITY_SUFFIXES):
            continue
        out.append(e)
    return out


def _merge_due_sources(due_payload, quiz_payload, calendar_events=None):
    """
    Fold the "what's due" feeds - content-linked due items, tool-native
    quizzes, and (optionally) course calendar events - into one flat list,
    deduped by GradeItemId (falling back to a normalized title). Each merged
    entry keeps a Sources list naming which feeds it came from and, per field,
    keeps the first non-null value seen.
    """
    entries = []
    by_gid = {}
    by_title = {}

    def add(source, name, due, start, end, grade_item_id, url, extra):
        gid = str(grade_item_id) if grade_item_id not in (None, "") else None
        tkey = _norm_title_key(name)
        entry = None
        if gid is not None:
            entry = by_gid.get(gid)
        if entry is None and tkey:
            entry = by_title.get(tkey)
        if entry is None:
            entry = {
                "Name": name,
                "DueDate": due,
                "StartDate": start,
                "EndDate": end,
                "GradeItemId": grade_item_id,
                "Url": url,
                "Sources": [],
            }
            entry.update(extra)
            entries.append(entry)
        else:
            fields = {
                "Name": name, "DueDate": due, "StartDate": start,
                "EndDate": end, "GradeItemId": grade_item_id, "Url": url,
            }
            fields.update(extra)
            for k, v in fields.items():
                if entry.get(k) in (None, "") and v not in (None, ""):
                    entry[k] = v
        if gid is not None:
            by_gid.setdefault(gid, entry)
        if tkey:
            by_title.setdefault(tkey, entry)
        if source not in entry["Sources"]:
            entry["Sources"].append(source)

    if isinstance(due_payload, dict):
        for o in due_payload.get("Objects") or []:
            if not isinstance(o, dict):
                continue
            add(
                "content",
                o.get("ItemName") or o.get("Name") or o.get("Title"),
                o.get("DueDate"), o.get("StartDate"), o.get("EndDate"),
                o.get("GradeItemId"),
                o.get("ItemUrl") or o.get("Url"),
                {"Completed": _is_completed(o)},
            )

    if isinstance(quiz_payload, dict):
        for q in quiz_payload.get("Objects") or []:
            if not isinstance(q, dict):
                continue
            add(
                "quiz",
                q.get("Name"),
                q.get("DueDate"), q.get("StartDate"), q.get("EndDate"),
                q.get("GradeItemId"), None,
                {"Type": "Quiz", "IsActive": q.get("IsActive")},
            )

    for e in calendar_events or []:
        if not isinstance(e, dict):
            continue
        entity_type = _calendar_entity_type(e)
        link = (e.get("AssociatedEntity") or {}).get("Link")
        start = e.get("StartDateTime")
        end = e.get("EndDateTime")
        add(
            "calendar",
            e.get("Title"),
            start, None, (end if end and end != start else None),
            None,
            link or e.get("CalendarEventViewUrl"),
            {"Type": _calendar_type_label(entity_type)},
        )

    return entries


def _normalize_orgids(orgids):
    """
    Accepts a list of orgids, a single orgid (str/int), a JSON-encoded string
    of a list, or a plain comma-separated string. Some MCP clients serialize
    array args in one of those string forms rather than a real array.
    Always returns a list of orgids as strings.
    """
    if isinstance(orgids, (list, tuple, set)):
        return [str(o) for o in orgids]

    if isinstance(orgids, str):
        stripped = orgids.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                import json
                parsed = json.loads(stripped)
                if isinstance(parsed, (list, tuple)):
                    return [str(o) for o in parsed]
            except Exception:
                pass
        # Also handle a plain comma-separated string, e.g. "123,456"
        if "," in stripped:
            return [part.strip() for part in stripped.split(",") if part.strip()]

    return [str(orgids)]


@mcp.tool()
async def getBatchWeeklyTodo(orgids: list[str] | str, days: int | str = 7, include_completed: bool | str = False):
    """
    Retrieves the calling user's scheduled content items (assignments, quizzes,
    readings, etc.) due within the next N days (default 7), across MULTIPLE
    courses in one call.

    `orgids` is a list of OrgUnit IDs (as found in the getClasses method). For
    client convenience it also accepts a single orgid, a JSON-array string, or a
    comma-separated string. `days` accepts an int or a numeric string (1..365).

    Already-completed items are filtered out server-side by default; pass
    include_completed=True to keep them. For everything outstanding with no date
    window use getBatchAllDueItems.

    COVERAGE LIMIT: content-linked items only - misses Quizzes-tool quizzes
    and calendar-only deadlines. Use getBatchEverythingDue for the full
    (content + quizzes + calendar) picture.

    Fetches all courses concurrently server-side and returns a dict mapping
    each orgid to its getWeeklyTodo-style response, e.g.:
        {
            "1630889": {"Objects": [...], "Next": null},
            "1641950": {"Objects": [...], "Next": null},
            ...
        }
    """
    ids = _normalize_orgids(orgids)
    days = _coerce_days(days)
    include_completed = _coerce_bool(include_completed)
    now = datetime.now(timezone.utc)
    params = {
        "startDateTime": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "endDateTime": (now + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }

    async def fetch_one(orgid):
        payload = await request(f'/le/1.82/{orgid}/content/myItems/due/', params=params)
        return orgid, _filter_due_payload(payload, include_completed)

    results = await asyncio.gather(*(fetch_one(orgid) for orgid in ids))
    return {orgid: result for orgid, result in results}


@mcp.tool()
async def getBatchAllDueItems(orgids: list[str] | str, include_completed: bool | str = False):
    """
    Retrieves the calling user's scheduled items across MULTIPLE courses in one
    call, with NO date window (past-due, this week, and everything further out).
    This is the multi-course form of getAllDueItems and the right tool for
    "what's due beyond this week".

    `orgids` accepts a list, a single orgid, a JSON-array string, or a
    comma-separated string. Already-completed items are filtered out server-side
    by default; pass include_completed=True to keep them. See getAllDueItems for
    the availability-field caveats.

    COVERAGE LIMIT: content-linked items only - misses Quizzes-tool quizzes
    and calendar-only deadlines. Use getBatchEverythingDue for the full
    (content + quizzes + calendar) picture.

    Returns a dict mapping each orgid to its getAllDueItems-style response.
    """
    ids = _normalize_orgids(orgids)
    include_completed = _coerce_bool(include_completed)

    async def fetch_one(orgid):
        payload = await request(f'/le/1.82/{orgid}/content/myItems/due/')
        return orgid, _filter_due_payload(payload, include_completed)

    results = await asyncio.gather(*(fetch_one(orgid) for orgid in ids))
    return {orgid: result for orgid, result in results}


@mcp.tool()
async def getBatchCalendarEvents(orgids: list[str] | str, days: int | str = 7):
    """
    Retrieves the calling user's calendar events within the next N days
    (default 7), across MULTIPLE courses in one call.

    `orgids` accepts a list, a single orgid, a JSON-array string, or a
    comma-separated string. `days` accepts an int or a numeric string (1..365).

    Fetches all courses concurrently server-side and returns a dict mapping
    each orgid to its getWeeklyCalendarEvents-style response, e.g.:
        {
            "1630889": {"Objects": [...], "Next": null},
            "1641950": {"Objects": [...], "Next": null},
            ...
        }
    """
    ids = _normalize_orgids(orgids)
    days = _coerce_days(days)
    now = datetime.now(timezone.utc)
    params = {
        "startDateTime": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "endDateTime": (now + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }

    async def fetch_one(orgid):
        return orgid, await request(f'/le/1.82/{orgid}/calendar/events/myEvents/', params=params)

    results = await asyncio.gather(*(fetch_one(orgid) for orgid in ids))
    return {orgid: result for orgid, result in results}


@mcp.tool()
async def getQuizzes(orgid, include_inactive: bool | str = False):
    """
    Retrieves every quiz object in a course straight from the Quizzes tool,
    independent of whether an instructor has linked it into course content.
    getWeeklyTodo / getAllDueItems / getCourseToc only see content-linked
    items, so a quiz that has live dates but has not been dragged into a
    module is INVISIBLE to them - this tool is how you find it.

    Input uses the OrgUnit ID found in the getClasses method. Each entry
    carries Name, StartDate, EndDate, DueDate, IsActive and GradeItemId
    (cross-check getAssignedGrades for submission / score status).

    Quizzes toggled off (IsActive false) are filtered out by default; pass
    include_inactive=True to keep them. The response is a D2L ObjectListPage
    ({"Objects": [...], "Next": ...}); a very large course may page.
    """
    include_inactive = _coerce_bool(include_inactive)
    payload = await request(f'/le/1.82/{orgid}/quizzes/')
    return _filter_quiz_payload(payload, include_inactive)


@mcp.tool()
async def getBatchQuizzes(orgids: list[str] | str, include_inactive: bool | str = False):
    """
    getQuizzes across MULTIPLE courses in one call - every quiz object per
    course, independent of content linkage (see getQuizzes for why that
    matters).

    `orgids` accepts a list, a single orgid, a JSON-array string, or a
    comma-separated string. Quizzes toggled off (IsActive false) are filtered
    out by default; pass include_inactive=True to keep them.

    Fetches all courses concurrently server-side and returns a dict mapping
    each orgid to its getQuizzes-style response.
    """
    ids = _normalize_orgids(orgids)
    include_inactive = _coerce_bool(include_inactive)

    async def fetch_one(orgid):
        payload = await request(f'/le/1.82/{orgid}/quizzes/')
        return orgid, _filter_quiz_payload(payload, include_inactive)

    results = await asyncio.gather(*(fetch_one(orgid) for orgid in ids))
    return {orgid: result for orgid, result in results}


def _calendar_window_params(days):
    """
    now-14d .. now+days window for /calendar/events/myEvents/. The 14-day
    lookback keeps just-passed deliverables visible alongside content's own
    past-due feed.
    """
    now = datetime.now(timezone.utc)
    return {
        "startDateTime": (now - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "endDateTime": (now + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }


@mcp.tool()
async def getEverythingDue(orgid, include_completed: bool | str = False, days: int | str = 180):
    """
    The complete "what's due" picture for one course: merges the three feeds
    that otherwise have to be called separately -

      * content-linked due items  (/content/myItems/due/       - getAllDueItems)
      * tool-native quizzes       (/quizzes/                    - getQuizzes)
      * course calendar events    (/calendar/events/myEvents/   - getWeeklyCalendarEvents)

    deduped by GradeItemId (then by normalized title). Use this when you need
    to be sure nothing is missed. Many deliverables surface in ONLY one feed:
    a quiz built in the Quizzes tool but never linked into content shows only
    in /quizzes/; a publisher/textbook "quiz" that is really a content topic,
    or a homework/lab whose due date lives only on the calendar, shows only in
    the calendar feed and is invisible to getAllDueItems / getWeeklyTodo.

    Input uses the OrgUnit ID found in the getClasses method. `days` (default
    180, 1..365) bounds the calendar feed's forward window; the content and
    quiz feeds have no window. Each merged entry carries Name, DueDate,
    StartDate, EndDate, GradeItemId, Type and a Sources list ("content",
    "quiz", "calendar"). Filter by DueDate client-side.

    Completed content items are dropped by default (pass include_completed=
    True to keep them); inactive quizzes are always excluded. Calendar events
    carry NO completion/submission state, so a calendar-only entry cannot be
    filtered that way - cross-check getAssignedGrades for score/submission
    status. Structural "module/week opens" calendar markers are dropped.
    """
    include_completed = _coerce_bool(include_completed)
    cal_params = _calendar_window_params(_coerce_days(days, default=180))
    due, quizzes, calendar = await asyncio.gather(
        request(f'/le/1.82/{orgid}/content/myItems/due/'),
        request(f'/le/1.82/{orgid}/quizzes/'),
        request(f'/le/1.82/{orgid}/calendar/events/myEvents/', params=cal_params),
    )
    return _merge_due_sources(
        _filter_due_payload(due, include_completed),
        _filter_quiz_payload(quizzes, include_inactive=False),
        _project_calendar_payload(calendar),
    )


@mcp.tool()
async def getBatchEverythingDue(orgids: list[str] | str, include_completed: bool | str = False, days: int | str = 180):
    """
    getEverythingDue across MULTIPLE courses in one call - the merged
    content-due + quizzes + calendar picture, deduped per course.

    `orgids` accepts a list, a single orgid, a JSON-array string, or a
    comma-separated string. `days` (default 180, 1..365) bounds each course's
    calendar window. Completed content items are dropped by default (pass
    include_completed=True to keep them); inactive quizzes are always excluded;
    module/week "opens" calendar markers are dropped. Calendar-only entries
    have no completion state - see getEverythingDue.

    Fetches every course's feeds concurrently server-side and returns a dict
    mapping each orgid to its getEverythingDue-style list.
    """
    ids = _normalize_orgids(orgids)
    include_completed = _coerce_bool(include_completed)
    cal_params = _calendar_window_params(_coerce_days(days, default=180))

    async def fetch_one(orgid):
        due, quizzes, calendar = await asyncio.gather(
            request(f'/le/1.82/{orgid}/content/myItems/due/'),
            request(f'/le/1.82/{orgid}/quizzes/'),
            request(f'/le/1.82/{orgid}/calendar/events/myEvents/', params=cal_params),
        )
        merged = _merge_due_sources(
            _filter_due_payload(due, include_completed),
            _filter_quiz_payload(quizzes, include_inactive=False),
            _project_calendar_payload(calendar),
        )
        return orgid, merged

    results = await asyncio.gather(*(fetch_one(orgid) for orgid in ids))
    return {orgid: result for orgid, result in results}


async def filter_current_courses(input):
    """
    Filter a Brightspace getClasses() response down to just the current,
    accessible Course Offerings (skipping old terms and duplicate
    Group/Lab sub-orgs), de-duped by course code.

    Args:
        raw: the raw dict returned by getClasses (must contain "Items").

    Returns:
        A list of {"id": ..., "name": ..., "code": ...} dicts.
    """
    seen = set()
    courses = []

    for item in input.get("Items", []):
        org_unit = item["OrgUnit"]
        access = item["Access"]

        if org_unit["Type"]["Code"] != "Course Offering":
            continue  # skip Group/Lab duplicates
        if not access.get("IsActive") or not access.get("CanAccess"):
            continue  # skip inactive or past-term courses

        code = org_unit["Code"]
        if code in seen:
            continue
        seen.add(code)

        courses.append({
            "id": org_unit["Id"],
            "name": org_unit["Name"],
            "code": code,
        })

    return courses

class RequireToken(BaseHTTPMiddleware):
    def __init__(self, app, token: str):
        super().__init__(app)
        self._token = token

    async def dispatch(self, request, call_next):
        auth = request.headers.get("authorization", "")
        supplied = auth[7:] if auth.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(supplied, self._token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)   # token good → hand off to MCP

def main() -> None:
    """Console-script entry point: serve the MCP tools over streamable-http.

    Bound to loopback; a front nginx proxy terminates TLS for
    https://<MCP_PUBLIC_HOST> (see deploy/brightspace-mcp.service).
    Requires MCP_INBOUND_TOKEN in the environment (enforced by RequireToken).
    """

    token = os.environ["MCP_INBOUND_TOKEN"]

    app = mcp.streamable_http_app(
        transport_security=TRANSPORT_SECURITY,
        host="127.0.0.1",
    )
    app.add_middleware(RequireToken, token=token)
    
    uvicorn.run(app, host="127.0.0.1", port=8008, log_level="info")


if __name__ == "__main__":
    main()