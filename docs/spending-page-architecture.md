# Spending page architecture

```mermaid
flowchart TB
    Browser["Browser<br/>http://127.0.0.1:8420/ui/spending"]
    MCP["q-core MCP client"]
    API["FastAPI / launchd-managed API process<br/>127.0.0.1:8420"]
    Static["Static UI assets<br/>api/static/<br/>spending.html + vendored Highcharts JS"]
    Periods["GET /spending/periods<br/>read-only reporting endpoint"]
    Coverage["GET /statement_coverage<br/>read-only reporting endpoint"]
    Tool["spending_periods MCP tool<br/>thin HTTP client"]
    Finance["Financial reporting service<br/>period grouping, coverage, merchant drill-down"]
    DB[("SQLite<br/>transactions · statements<br/>categories · accounts")]

    Browser -->|"GET /ui/spending<br/>no bearer token"| API
    API --> Static
    Static -->|"Highcharts renders locally"| Browser

    Browser -->|"fetch JSON<br/>no bearer token"| Periods
    Browser -->|"fetch JSON<br/>no bearer token"| Coverage

    MCP -->|"authenticated MCP"| API
    API --> Tool
    Tool -->|"authenticated loopback HTTP"| Periods

    Periods --> Finance
    Coverage --> Finance
    Finance --> DB
```

Only `/ui/spending`, `/spending/periods`, and the coverage read endpoint are
exempt from bearer authentication; the exemption is a pinned read-only
allowlist. `/spending/periods` is the single source of truth for the page and
the MCP `spending_periods` tool.
