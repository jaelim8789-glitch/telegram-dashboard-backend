"""Create stub migration for missing revision b1d2e3f4a5b6."""
import os

versions_dir = "/opt/telemon/backend/alembic/versions"
stub_file = os.path.join(versions_dir, "b1d2e3f4a5b6_stub.py")

# Find what migration b1d2e3f4a5b6 should revise
# Look at a7b8c9d0e1f2_add_referral_tables.py for context
with open(os.path.join(versions_dir, "a7b8c9d0e1f2_add_referral_tables.py")) as f:
    content = f.read()
    
# Extract revises from the file that references b1d2e3f4a5b6
import re
revises_match = re.search(r'revises?\s*=\s*["\']([^"\']+)["\']', content)
if revises_match:
    revises_value = revises_match.group(1)
    print(f"a7b8c9d0e1f2 revises: {revises_value}")

# Find migrations that b1d2e3f4a5b6 should revise (its parent)
# Look for the latest migration before the referral tables
parent_revision = None
latest_date = ""
for f in sorted(os.listdir(versions_dir)):
    if not f.endswith(".py"):
        continue
    if f == "b1d2e3f4a5b6_stub.py":
        continue
    with open(os.path.join(versions_dir, f)) as fh:
        fc = fh.read()
    rev = re.search(r'revision\s*=\s*["\']([^"\']+)["\']', fc)
    if rev:
        rev_id = rev.group(1)
        # Check if any migration revises this one
        is_revised = False
        for f2 in sorted(os.listdir(versions_dir)):
            if not f2.endswith(".py") or f2 == f:
                continue
            with open(os.path.join(versions_dir, f2)) as fh2:
                fc2 = fh2.read()
            if f'revises?\s*=\s*["\']{rev_id}["\']' in fc2.replace(' ', ''):
                is_revised = True
                break
        if not is_revised and rev_id != "a7b8c9d0e1f2":
            parent_revision = rev_id
            print(f"Found head migration: {rev_id} ({f})")

# Read the referral migration's create date to determine correct ordering
date_match = re.search(r'Create Date:\s*([\d\- \.:]+)', content)
if date_match:
    print(f"Referral migration date: {date_match.group(1).strip()}")

print(f"Parent revision for stub: {parent_revision}")

# Create stub migration
stub_content = f'''"""Stub migration for revision b1d2e3f4a5b6 (placeholder)."""

revision = "b1d2e3f4a5b6"
down_revision = {repr(parent_revision)}
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
'''

with open(stub_file, "w") as f:
    f.write(stub_content)

print(f"Created stub: {stub_file}")
