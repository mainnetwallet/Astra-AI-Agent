# ASTRA System Health — UI Design Reference

Static, self-contained design reference for the ASTRA **System Health** dashboard.

- `astra-system-health.html` — the full dashboard (single file, no build step, no dependencies)
- `astra-system-health-preview.png` — 1920x1080 (16:9) render

## What this is

A visual target for the System Health surface: sidebar + topbar shell, page header with
tabs and auto-refresh controls, six KPI cards, a health summary row, Core Services,
AI Provider Health, System Resources, and the full-width AI Model Health table
(provider, model, key tag, status, latency, success, last check, capabilities).

## What this is not

It is **not** wired into the running application. Open the HTML file directly in a
browser. All content comes from the labelled `DATA` object at the top of the inline
script (services, providers, resources, models) — illustrative sample content chosen
to match the design brief.

The live backend cannot currently produce this view:

- `/api/providers` returns an empty object and `/api/metrics` reports 0 providers / 0 models
- host CPU / memory / disk / network metrics are unavailable (`psutil` is not installed)
- there are no Redis, Vector DB, File Storage or per-model health/latency endpoints
- provider credentials are never exposed by the API (only counts)

To wire it up, replace `DATA` with normalized responses from the real health endpoints
and hide any field the backend does not provide.

## Safety

No real credentials appear anywhere in this reference. Key columns render safe
identifiers only (`GEMINI_KEY_1`, `CLOUDFLARE_KEY_2`, `HF_KEY_1`, ...). All rendered
strings are HTML-escaped.