# Brightspace-MCP

An [MCP](https://modelcontextprotocol.io) server that can expose your Brightspace account as tools available for an LLM to call. 

Some assignments confound me in format and feed because every professor has a different way of assigning things, being a quiz, a content item, something on the course calendar, or even something else. Each feed is individually incomplete when calling the API so there's availability to call batch functions for multi entry retrieval.

## Tools

--claude add description for tools and watermark this as claude written

## Architechture

```
Claude / MCP client
      │  HTTPS  (no inbound auth — see below)
      ▼
nginx  (mcp.yourdomain.com, TLS termination) ← My setup, http streamable claude requires HTTPS so I used Cloudflare
      │  HTTP, Host preserved
      ▼
brightspacemcp  (streamable-http, 127.0.0.1:8008)   ← this repo
      │  session cookies + browser UA
      ▼
purdue.brightspace.com/d2l/api
```