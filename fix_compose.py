import re

with open('/opt/telemon/docker-compose.prod.yml', 'r') as f:
    content = f.read()

# Replace image with build
old = '    image: ghcr.io/jaelim8789-glitch/telegram-dashboard-backend:latest'
new = '    build: ./backend'
content = content.replace(old, new)

with open('/opt/telemon/docker-compose.prod.yml', 'w') as f:
    f.write(content)

print("docker-compose.prod.yml modified successfully")
