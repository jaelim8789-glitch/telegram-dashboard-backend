with open("/opt/telemon/docker-compose.prod.yml", "r") as f:
    content = f.read()

# Add command after env_file line
old = "    env_file: ./backend/.env\n    container_name: telemon_backend_prod"
new = '    env_file: ./backend/.env\n    command: uvicorn backend.main:app --host 0.0.0.0 --port 8000\n    container_name: telemon_backend_prod'

if old in content:
    content = content.replace(old, new)
    with open("/opt/telemon/docker-compose.prod.yml", "w") as f:
        f.write(content)
    print("CMD override added")
else:
    print("Pattern not found")

# Verify
with open("/opt/telemon/docker-compose.prod.yml", "r") as f:
    lines = f.readlines()
in_backend = False
for line in lines:
    if line.strip() == "  backend:":
        in_backend = True
    if in_backend:
        print(line.rstrip())
        if not line.startswith(" ") and line.strip():
            in_backend = False
