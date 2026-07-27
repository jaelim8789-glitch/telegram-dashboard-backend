with open("/opt/telemon/docker-compose.prod.yml", "r") as f:
    content = f.read()

old = "    command: uvicorn backend.main:app --host 0.0.0.0 --port 8000"
new = "    command: uvicorn app.main:app --host 0.0.0.0 --port 8000"

if old in content:
    content = content.replace(old, new)
    with open("/opt/telemon/docker-compose.prod.yml", "w") as f:
        f.write(content)
    print("Fixed command")
else:
    print("Old pattern not found, checking current state...")
    # Show the relevant section
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if "command:" in line and "backend" not in lines[i-1] if i > 0 else True:
            print(f"Line {i}: {line}")
        if "build:" in line and "./backend" in line:
            for j in range(i, min(i+5, len(lines))):
                print(f"Line {j}: {lines[j]}")

with open("/opt/telemon/docker-compose.prod.yml", "r") as f:
    content = f.read()
# Verify
for line in content.split("\n"):
    if "command:" in line or "main:" in line:
        print(f"  >> {line.strip()}")
