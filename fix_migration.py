import os

path = "/opt/telemon/backend/alembic/versions/a7b8c9d0e1f2_add_referral_tables.py"
with open(path, "r") as f:
    content = f.read()

# Remove stub if exists
stub = "/opt/telemon/backend/alembic/versions/b1d2e3f4a5b6_stub.py"
if os.path.exists(stub):
    os.remove(stub)
    print("Removed stub")

# Fix the revises reference
old = 'revises = "b1d2e3f4a5b6"'
new = 'revises = "c9f1e3a5b7d2"'
if old in content:
    content = content.replace(old, new)
    with open(path, "w") as f:
        f.write(content)
    print(f"Fixed: {old} -> {new}")
else:
    print(f"Could not find: {old}")

# Verify the fix
with open(path, "r") as f:
    first_lines = "".join(f.readlines()[:10])
print(f"Current state:\n{first_lines}")
