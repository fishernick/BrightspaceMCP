# Brightspace MCP

An [MCP](https://modelcontextprotocol.io) server that exposes a Purdue
Brightspace (D2L Valence) account as tools an LLM can call: enrollments, grades,
course contents, calendar, quizzes, and a single merged "what's due" view.

It exists because "what do I actually have due?" has no single answer in
Brightspace — the deadline for one deliverable can live in any of three
disconnected places, and no built-in screen shows all three at once. This
server queries all of them and reconciles the results.

## The problem it solves

A gradeable item in Brightspace can surface through any combination of:

| Feed | Endpoint | What only it sees |
| --- | --- | --- |
| **Content** | `/content/myItems/due/` | items an instructor linked into a module |
| **Quizzes tool** | `/quizzes/` | a quiz with live dates that was never linked into content |
| **Course calendar** | `/calendar/events/myEvents/` | a homework/lab whose due date was only put on the calendar, or a publisher "quiz" that is really a content topic |

Each feed is individually incomplete, uses different field names, and carries a
different subset of state (only content items have completion status; calendar
events have none). `getEverythingDue` / `getBatchEverythingDue` fetch all three
concurrently and fold them into one list, deduped by `GradeItemId` (falling back
to a normalized title), with a `Sources` list on each entry recording which
feeds it came from. The per-tool docstrings spell out each feed's coverage
limits.

## Tools

All course-scoped tools take an `orgid` (OrgUnit ID) from `getClasses`. `Batch`
variants take a list of orgids (also accepting a JSON-array or comma-separated
string, since some MCP clients stringify array args) and fan out concurrently.

**Account**
- `getUser` — whoami
- `getClasses` — current enrollments (pass `false` for a de-duped, current-term-only list; `true` for the raw feed)

**Grades**
- `getAssignedGrades` / `getAllGrades` — grade values, or the full grade object, for a course

**What's due**
- `getWeeklyTodo` / `getBatchWeeklyTodo` — content-linked items due in the next N days
- `getAllDueItems` / `getBatchAllDueItems` — content-linked items, no date window
- `getWeeklyCalendarEvents` / `getBatchCalendarEvents` — calendar events in the next N days
- `getQuizzes` / `getBatchQuizzes` — every quiz in the Quizzes tool, regardless of content linkage
- `getEverythingDue` / `getBatchEverythingDue` — the merged content + quizzes + calendar picture

**Content**
- `getCourseToc` — flattened table of contents (modules/topics, availability fields); `full=true` for the raw nested tree

Completed content items and inactive quizzes are filtered out by default;
`include_completed` / `include_inactive` keep them.

## Architecture

```
Claude / MCP client
      │  HTTPS  (no inbound auth — see below)
      ▼
nginx  (mcp.xennick.com, TLS termination)
      │  HTTP, Host preserved
      ▼
brightspacemcp  (streamable-http, 127.0.0.1:8008)   ← this repo
      │  session cookies + browser UA
      ▼
purdue.brightspace.com/d2l/api
```

- **Transport:** `streamable-http` bound to loopback. `TransportSecuritySettings`
  pins `allowed_hosts` / `allowed_origins` so the SDK's DNS-rebinding protection
  accepts the `Host` nginx forwards.
- **Inbound auth (MCP client → this server):** **none.** The server is
  constructed with no `auth` / `token_verifier`, so the `/mcp/` endpoint accepts
  any request that reaches it (loopback + the nginx proxy); `main()` logs a
  warning at startup. Authentication is expected to be added later on the
  `MCPServer(...)` in `server.py`.
- **Outbound auth (this server → Brightspace):** `auth.return_cookies()` reads
  `d2lSessionVal` / `d2lSecureSessionVal` from `.env` (via `python-dotenv`).
  Those are a logged-in browser session's cookies; a separate process is
  expected to refresh them into `.env`. Requests also send a desktop-browser
  `User-Agent`.
- **Deploy:** `deploy/brightspace-mcp.service` runs the `brightspacemcp` console
  script under systemd with `uv run --frozen --no-sync` (never touches the
  lockfile at boot) and a strict sandbox (`ProtectSystem=strict`,
  `ProtectHome=read-only`, restricted address families, private tmp).

### Caveats

The **inbound** side has no authentication, and the **outbound** side
talks to the Valence API with a **scraped session cookie**, not a registered
OAuth 2.0 app, so it is single-user, tied to one `purdue.brightspace.com`
account, breaks when the session expires, and may be against Purdue's API terms.
It is a personal automation tool, not a multi-tenant service. A production
version would also register a D2L app and use the Valence OAuth flow for the
Brightspace call.

## Setup

Requires Python ≥ 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

Create `.env` with a current browser session's cookies:

```
d2lSessionVal=...
d2lSecureSessionVal=...
```

(Copy the `d2l*` cookies from your browser's dev tools while logged into
`purdue.brightspace.com` — cookies named `d2lSessionVal` and
`d2lSecureSessionVal`.)

There is **no inbound authentication** — the `/mcp/` endpoint is open to
anything that can reach it, so keep it behind the loopback bind / nginx proxy
and don't expose it further until auth is added.

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

thank you claude for writing my readme <3