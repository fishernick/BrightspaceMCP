# Brightspace-MCP

An [MCP](https://modelcontextprotocol.io) server that can expose your Brightspace account as tools available for an LLM to call. 

Some assignments confound me in format and feed because every professor has a different way of assigning things, being a quiz, a content item, something on the course calendar, or even something else. Each feed is individually incomplete when calling the API so there's availability to call batch functions for multi entry retrieval.

## Tools

> _This section was written by Claude from the tool docstrings in
> [`server.py`](src/brightspacemcp/server.py)._

All course-scoped tools take an `orgid` (the OrgUnit ID) from `getClasses`.
`getBatch*` variants take a list of orgids — a real array, a JSON-array string,
or a comma-separated string, since some MCP clients stringify array args — and
fan out across courses concurrently.

**Account**
- `getUser` — the calling user's profile (`whoami`).
- `getClasses` — current enrollments. Pass `false` for a de-duped,
  current-term-only list (`id` / `name` / `code`); pass `true` for the raw D2L
  enrollments feed including past and hidden courses. Prefer `false` to save
  tokens.

**Grades**
- `getAssignedGrades` — the user's grade *values* for one course.
- `getAllGrades` — the full grade objects (definitions + values) for one course.

**What's due**
- `getWeeklyTodo` / `getBatchWeeklyTodo` — content-linked items due in the next
  `days` (default 7). Each item has `ItemName`, `DueDate`, `ItemUrl`.
- `getAllDueItems` / `getBatchAllDueItems` — content-linked items with no date
  window (past-due through everything upcoming).
- `getWeeklyCalendarEvents` / `getBatchCalendarEvents` — course calendar events
  in the next `days` (default 7), each with a `CalendarEventViewUrl`.
- `getQuizzes` / `getBatchQuizzes` — every quiz in the Quizzes tool, whether or
  not an instructor linked it into content. Inactive quizzes are hidden unless
  `include_inactive=true`.
- `getEverythingDue` / `getBatchEverythingDue` — the merged content + quizzes +
  calendar picture, deduped by `GradeItemId` (then normalized title). Each entry
  carries a `Sources` list naming the feeds it came from. `days` (default 180)
  bounds only the calendar window. Use this when nothing can be missed.

**Content**
- `getCourseToc` — the course table of contents. Default is a flat list of
  topics (module path, `Type`, `Url`, `GradeItemId`, availability fields);
  `full=true` returns the raw nested D2L tree. The TOC has no due dates or
  completion state.

Completed content items and inactive quizzes are filtered out by default;
`include_completed` / `include_inactive` bring them back. Calendar events carry
no completion state, so a calendar-only entry can't be filtered that way —
cross-check `getAssignedGrades` for submission/score status.

## Architecture

```
Claude / MCP client
      │  HTTPS  + Authorization: Bearer <token>
      ▼
nginx  ($MCP_PUBLIC_HOST, TLS termination) ← My setup, http streamable claude requires HTTPS so I used Cloudflare
      │  HTTP, Host preserved
      ▼
brightspacemcp  (streamable-http, 127.0.0.1:8008)   ← this repo
      │  session cookies + browser UA
      ▼
purdue.brightspace.com/d2l/api
```

- **Transport:** `streamable-http` bound to loopback. `TransportSecuritySettings`
  pins `allowed_hosts` / `allowed_origins` (from `MCP_PUBLIC_HOST`, default
  `mcp.xennick.com`) so the SDK's DNS-rebinding protection accepts the `Host`
  nginx forwards.
- **Inbound auth (MCP client → this server):** the `RequireToken` middleware
  rejects any request without `Authorization: Bearer <MCP_INBOUND_TOKEN>`
  (constant-time compare); the token lives in `.env`.
- **Outbound auth (this server → Brightspace):** `auth.return_cookies()` reads
  `d2lSessionVal` / `d2lSecureSessionVal` from `.env` (via `python-dotenv`).
  Those are a logged-in browser session's cookies; a separate process is
  expected to refresh them into `.env`. Requests also send a desktop-browser
  `User-Agent`.
- **Deploy:** `deploy/brightspace-mcp.service` runs the `brightspacemcp` console
  script under systemd with `uv run --frozen --no-sync` (never touches the
  lockfile at boot) and a strict sandbox (`ProtectSystem=strict`,
  `ProtectHome=read-only`, restricted address families, private tmp).

### Caveat

My inbound side has self-generated token auth at the moment and the outbound side talks to the D2L api with a scraped session cookie, not an OAuth 2.0 app.   
**"Unfortunately, we have not yet provided students with OAuth 2.0 access to Brightspace for personal use."**  
Because of this, it is single-user, tied to a single account, breaks on session expiry, and is likely against API terms. It is a personal tool, not a multi-user service. A future production version would also be able to register a D2L app and use the OAuth flow for the Brightspace call.

## Setup

Requires Python ≥ 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

Create `.env` with a current browser session's cookies:

```
d2lSessionVal=...
d2lSecureSessionVal=...
MCP_INBOUND_TOKEN=...        # bearer token MCP clients must send
MCP_PUBLIC_HOST=...          # optional; public hostname nginx serves (default mcp.xennick.com)
```

(Copy the `d2l*` cookies from your browser's dev tools while logged into
`purdue.brightspace.com` — cookies named `d2lSessionVal` and
`d2lSecureSessionVal`.)

Run the server:

```bash
uv run brightspacemcp          # or: python -m brightspacemcp
```

It listens on `http://127.0.0.1:8008`. Point an MCP client at that (directly, or
through a TLS proxy as above).

### Deploy as a service

```bash
cp deploy/brightspace-mcp.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now brightspace-mcp.service
```

Adjust `User`, `WorkingDirectory`, and the `uv` path in the unit first. After
changing dependencies, run `uv sync` by hand — the unit deliberately runs
`--frozen --no-sync`.

## Layout

```
src/brightspacemcp/
  __init__.py   exports main()
  __main__.py   python -m brightspacemcp
  server.py     MCPServer, all @mcp.tool() definitions, feed-merge logic
  auth.py       Brightspace session cookies from .env (outbound)
deploy/
  brightspace-mcp.service
pyproject.toml  uv_build, src layout, `brightspacemcp` console script
```
