#!/usr/bin/env python3
"""One-shot smoke test for Astra AI Agent: boots the real server the same way
run.py does (astra.bootstrap.build), exercises every major endpoint over HTTP
(core + plugin), prints results, shuts down. Exit 0 = green."""
import sys, os, threading, time, json, urllib.request, urllib.error
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

from astra.store import Store
from astra.bootstrap import build
from astra.web import AstraServer

PORT = 9876
failures = 0


def get(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}") as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode()
            return e.code, (json.loads(raw) if raw else {})
        finally:
            e.close()


def send(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data,
                                 method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode()
            return e.code, (json.loads(raw) if raw else {})
        finally:
            e.close()


def check(label, cond):
    global failures
    print(f"  {'✅' if cond else '❌ FAIL'}  {label}")
    if not cond:
        failures += 1


def main():
    print("Boot Astra server (full stack)... ", end="", flush=True)
    stack = build(store=Store(":memory:"), with_scheduler=True)
    store = stack["store"]; agent = stack["agent"]
    plugin_objs = stack["plugins"]
    httpd = AstraServer(("127.0.0.1", PORT), store, agent, plugin_objs, stack=stack)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.3)
    print("OK\n")

    # 0. core system endpoints (new in the Personal-OS upgrade)
    s, body = get("/api/health")
    check("health 200 + ok", s == 200 and body["data"]["ok"] is True
          and "database" in body["data"]["checks"])
    s, body = get("/api/plugins")
    check("plugins listing has airdrop", "airdrop" in
          [p["slug"] for p in body["data"]])
    s, body = get("/api/tools")
    check("tool registry 13 builtins", len(body["data"]["tools"]) >= 13
          and "search_web" in [t["name"] for t in body["data"]["tools"]])
    s, body = get("/api/providers")
    check("offline provider not registered", "offline" not in body["data"]["providers"])
    s, body = get("/api/events")
    check("events endpoint returns list", isinstance(body["data"], list))

    # task engine (generic, not plugin tasks)
    s, body = send("POST", "/api/tasks", {"goal": "daily check", "type": "research"})
    check("task engine create", s == 201 and body["data"]["id"] > 0)
    s, body = get("/api/tasks")
    check("task engine list", any(t["type"] == "research" for t in body["data"]))

    # memory
    s, body = send("POST", "/api/memory",
                   {"content": "hamster eligibility tier 1", "category": "note"})
    check("memory save", s == 201 and body["data"]["id"] > 0)
    s, body = get("/api/memory/search?query=eligibility&k=1")
    check("memory search recall", body["data"] and "eligibility" in body["data"][0]["content"])

    # orchestrator (goal -> execution)
    s, body = send("POST", "/api/agents", {"goal": "amasader kaler kaj ki",
                                           "sync": True})
    check("agent goal submit", body["data"]["status"] in ("COMPLETED", "FAILED"))
    s, body = get("/api/executions")
    check("executions history", len(body["data"]) >= 1)

    # workflows
    s, body = send("POST", "/api/workflows",
                   {"name": "daily check", "steps": [{"tool": "get_health", "name": "h"}]})
    check("workflow define", s == 201 and body["data"]["id"] > 0)
    wf_id = body["data"]["id"]
    s, body = send("POST", f"/api/workflows/{wf_id}/run", {"params": {}})
    check("workflow run completes", body["data"]["status"] == "completed")

    # scheduler
    s, body = send("POST", "/api/schedules",
                   {"name": "morning", "kind": "daily", "value": "09:15"})
    check("schedule add", s == 201 and body["data"]["enabled"] == 1)
    s, body = send("PATCH", f"/api/schedules/{body['data']['id']}", {"enabled": False})
    check("schedule disable", body["data"]["enabled"] == 0)

    # 1. static + manifest
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/") as r:
        html = r.read().decode()
    check("index.html served (Astra shell)", "Astra" in html and "dash-blocks" in html)
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/static/js/astra.js") as r:
        check("astra.js served", r.read().decode().startswith("/* Astra AI Agent"))
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/static/js/plugins/airdrop.js") as r:
        check("airdrop plugin js served", "Astra.register" in r.read().decode())
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/static/css/style.css") as r:
        check("style.css served", "--bg:" in r.read().decode())

    s, m = get("/api/manifest")
    check("manifest 200 + name", s == 200 and m["data"]["name"] == "Astra AI Agent")
    check("manifest lists airdrop plugin", "airdrop" in
          [p["slug"] for p in m["data"]["plugins"]])
    tabs = [t["tab"] for t in m["data"]["tabs"]]
    check("manifest has core tabs", "assistant" in tabs and "backup" in tabs
          and "dashboard" in tabs and "live" in tabs and "airdrop" in tabs)

    # 2. dashboard (empty)
    s, body = get("/api/dashboard")
    check("dashboard 200", s == 200)
    ad = next(b for b in body["data"] if b["slug"] == "airdrop")
    check("dashboard empty airdrops card", ad["data"]["cards"][0]["v"] == 0)

    # 3. create airdrop via API
    s, body = send("POST", "/api/airdrops",
                   {"name": "Hamster", "deadline": "2026-12-31", "network": "TON"})
    check("create airdrop 201", s == 201)
    aid = body["data"]["id"]

    s, body = get("/api/airdrops")
    check("list airdrops has 1", len(body["data"]) == 1 and body["data"][0]["name"] == "Hamster")

    s, body = send("PATCH", f"/api/airdrops/{aid}", {"status": "farming"})
    check("patch status", s == 200 and body["data"]["status"] == "farming")

    # 4. tasks
    s, body = send("POST", "/api/tasks", {"airdrop_id": aid, "title": "join tg",
                                          "category": "social"})
    check("create task 201", s == 201 and body["data"]["status"] == "pending")
    tid = body["data"]["id"]
    s, body = send("PATCH", f"/api/tasks/{tid}", {"status": "done"})
    check("mark task done", body["data"]["status"] == "done")

    # 5. dashboard after data
    s, body = get("/api/dashboard")
    ad = next(b for b in body["data"] if b["slug"] == "airdrop")
    check("dashboard total=1", ad["data"]["cards"][0]["v"] == 1)

    # 6. wallets
    s, body = get("/api/wallet/validate?address=0x7A1234FF00AAAAAA567890")
    check("valid hex address", body["data"]["valid"] is True)
    s, body = get("/api/wallet/validate?address=abc")
    check("invalid short rejected", body["data"]["valid"] is False)
    s, body = send("POST", "/api/wallets",
                   {"address": "0x7A1234FF00AAAAAA567890", "label": "main100", "network": "ETH"})
    check("create wallet 201", s == 201 and body["data"]["label"] == "main100")
    s, body = send("POST", "/api/wallets", {"address": "too-short"})
    check("invalid wallet 400", s == 400)

    # 7. chat
    s, body = send("POST", "/api/chat",
                   {"message": "add airdrop Notcoin deadline 2026-09-30 reward points value 300"})
    check("chat add airdrop", body["data"]["ok"] and "Notcoin" in body["data"]["reply"])
    s, body = send("POST", "/api/chat", {"message": "list airdrops"})
    check("chat list airdrops", "Hamster" in body["data"]["reply"]
          and "Notcoin" in body["data"]["reply"])
    s, body = send("POST", "/api/chat", {"message": "progress"})
    check("chat progress", body["data"]["ok"] and "2" in body["data"]["reply"])
    s, body = send("POST", "/api/chat", {"message": "deadlines this month"})
    check("chat deadlines", body["data"]["ok"] and "Notcoin" in body["data"]["reply"])
    s, body = send("POST", "/api/chat", {"message": "ete bojhi"})
    check("unknown chat graceful", body["data"]["ok"] is False
          and "help" in body["data"]["reply"].lower())
    s, body = send("POST", "/api/chat", {"message": "help"})
    check("chat help mentions airdrop", "airdrop" in body["data"]["reply"].lower())

    # 8. export / import
    s, body = get("/api/export")
    payload = body["data"]
    check("export has airdrop data", "airdrop" in payload["_exports"]
          and len(payload["_exports"]["airdrop"]["airdrops"]) == 2)
    s, body = send("POST", "/api/import", {"data": payload})
    check("import dedupes", body["data"].get("airdrop_added_airdrops", 1) == 0)

    # 9. 404s
    s, _ = get("/api/nope")
    check("404 unknown route", s == 404)

    # 10. delete via chat
    s, body = send("POST", "/api/chat", {"message": "delete airdrop Notcoin"})
    check("delete via chat", body["data"]["ok"])
    s, body = get("/api/airdrops")
    check("only Hamster left", len(body["data"]) == 1)

    # ---- summary ----
    httpd.shutdown()
    print(f"\n{'='*40}")
    if failures == 0:
        print("  ALL CHECKS PASSED ✅")
    else:
        print(f"  {failures} CHECK(S) FAILED ❌")
    print(f"{'='*40}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()