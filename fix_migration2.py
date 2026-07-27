import os

path = "/opt/telemon/backend/alembic/versions/a7b8c9d0e1f2_add_referral_tables.py"
with open(path, "r") as f:
    content = f.read()

old = 'down_revision: Union[str, None] = "b1d2e3f4a5b6"'
new = 'down_revision: Union[str, None] = "c9f1e3a5b7d2"'

if old in content:
    content = content.replace(old, new)
    with open(path, "w") as f:
        f.write(content)
    print(f"Fixed: {old} -> {new}")
else:
    print(f"Could not find: {old}")
    # Try backup search
    import re
    match = re.search(r'down_revision.*=.*"b1d2e3f4a5b6"', content)
    if match:
        print(f"Found with regex: {match.group()}")
    else:
        print("No match found at all")
