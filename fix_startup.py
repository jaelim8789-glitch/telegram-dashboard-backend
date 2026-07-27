import sys

path = "/opt/telemon/docker-compose.prod.yml"
with open(path, "r") as f:
    lines = f.readlines()

# Find the backend service and add a command override after the env_file line
new_lines = []
in_backend = False
added_cmd = False

for line in lines:
    new_lines.append(line)
    if line.strip() == "    env_file: ./backend/.env":
        new_lines.append("    command: sh -c \"uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}\"\n")
        added_cmd = True

if added_cmd:
    with open(path, "w") as f:
        f.writelines(new_lines)
    print("CMD override added")
else:
    print("env_file not found")

# Show the backend section
print("\n=== Backend section ===")
in_backend = False
for line in lines:
    if line.strip() == "  backend:":
        in_backend = True
    if in_backend and line.startswith(" ") and len(line.strip()) > 0:
        print(line.rstrip())
    elif in_backend and not line.startswith(" ") and line.strip():
        break
