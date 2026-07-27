import sys
with open('/opt/telemon/docker-compose.prod.yml', 'r') as f:
    content = f.read()

content = content.replace(
    '    build: ./backend',
    '    build: ./backend\n    env_file: ./backend/.env'
)

with open('/opt/telemon/docker-compose.prod.yml', 'w') as f:
    f.write(content)
print("OK")
