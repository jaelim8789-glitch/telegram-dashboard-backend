import urllib.request, json, sys

base = "http://localhost:8000"
endpoints = [
    ("/health", "Health Check"),
    ("/openapi.json", "OpenAPI Schema"),
    ("/api/referrals/leaderboard", "Leaderboard (public)"),
    ("/api/referrals/my-code", "My Code (no auth)"),
    ("/api/referrals/admin/pending", "Admin Pending (no auth)"),
    ("/api/referrals/stats", "Stats (no auth)"),
]

for path, desc in endpoints:
    try:
        req = urllib.request.Request(base + path)
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = resp.status
            body = resp.read().decode()
            if len(body) > 200:
                body = body[:200] + "..."
            print(f"[{status}] {desc} ({path})")
            if path == "/health":
                print(f"  Body: {body}")
            elif path == "/openapi.json":
                data = json.loads(body)
                paths = list(data.get("paths", {}).keys())
                ref_paths = [p for p in paths if "referral" in p.lower()]
                print(f"  Total paths: {len(paths)}")
                print(f"  Referral paths ({len(ref_paths)}):")
                for p in ref_paths[:15]:
                    methods = list(data["paths"][p].keys())
                    print(f"    {p} [{','.join(methods)}]")
    except urllib.request.HTTPError as e:
        print(f"[{e.code}] {desc} ({path})")
        body = e.read().decode()
        if len(body) > 100:
            body = body[:100] + "..."
        print(f"  Error: {body}")
    except Exception as e:
        print(f"[ERR] {desc} ({path}): {e}")
