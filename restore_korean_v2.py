"""
Byte-level Korean string restoration from git history.

Strategy (BYTE-LEVEL, no string encoding issues):
  1. Read original from git show HEAD as raw bytes
  2. Read current file as raw bytes (pure ASCII)
  3. Normalize original line endings: \\r\\r\\n -> \\n, \\r\\n -> \\n (in bytes)
  4. Split both into byte-lines by \\n
  5. For each original byte-line with non-ASCII bytes:
     a. Create an ASCII-only version by stripping bytes > 127
     b. Find matching line in current file (by ASCII comparison)
     c. Replace with original byte-line (which preserves Korean UTF-8)
  6. Write back as bytes
"""

import subprocess
import os
import sys
import re

# Critical files that need Korean string restoration
CRITICAL_FILES = [
    # === AI PROMPTS (app/services/) ===
    "app/services/ai_chatbot_service.py",
    "app/services/ai_chat_service.py",
    "app/services/ai_analysis_service.py",
    "app/services/ai_content_studio_service.py",
    "app/services/ai_core_service.py",
    "app/services/ai_growth_coach_service.py",
    "app/services/ai_ops_service.py",
    "app/services/ai_reply_service.py",
    "app/services/ai_spam_guard_service.py",
    "app/services/ai_style_service.py",
    "app/services/bot_ai_agent_service.py",
    "app/services/billing.py",
    "app/services/chat_service.py",
    "app/services/deepseek_service.py",
    "app/services/delivery.py",
    "app/services/guide_hub_service.py",
    "app/services/lead_capture.py",
    "app/services/mcp_gateway.py",
    "app/services/telemon_memory_service.py",
    "app/services/usage_tracker.py",
    "app/services/usdt_watcher.py",
    "app/services/referral.py",
    "app/services/langgraph/supervisor.py",
    "app/services/auto_reply_service.py",
    "app/services/random_reply_service.py",
    "app/services/broadcast_processor.py",
    "app/services/failure_intel.py",
    "app/services/group_search_service.py",
    "app/services/join_queue_service.py",
    "app/services/link_inspector_service.py",
    "app/services/telegram_bot_service.py",
    "app/services/telegram_actions.py",
    "app/services/media.py",
    "app/services/sms_service.py",
    "app/services/bot_account_service.py",
    "app/services/bot_api_key_service.py",
    "app/services/nowpayments.py",
    "app/services/performance_card.py",
    "app/services/purchase_service.py",
    # === USER-FACING MESSAGES (app/api/) ===
    "app/api/account_self_reset.py",
    "app/api/accounts.py",
    "app/api/admin.py",
    "app/api/auth.py",
    "app/api/auto_reply.py",
    "app/api/billing.py",
    "app/api/chat.py",
    "app/api/deps.py",
    "app/api/folder.py",
    "app/api/groups.py",
    "app/api/group_search.py",
    "app/api/link_inspector.py",
    "app/api/preview.py",
    "app/api/referral.py",
    "app/api/reply_macro.py",
    "app/api/schedule.py",
    "app/api/scheduler.py",
    "app/api/telegram_auth.py",
    "app/api/tokens.py",
    "app/api/usdt_payment.py",
    "app/api/users.py",
    "app/api/ai_agent.py",
    "app/api/ai_assist.py",
    "app/api/features.py",
    "app/api/broadcast.py",
    "app/api/campaign.py",
    "app/api/channel_hub.py",
    "app/api/chats.py",
    "app/api/content_studio.py",
    "app/api/free_api_key.py",
    "app/api/growth_loop.py",
    "app/api/join_queue.py",
    "app/api/message_template.py",
    "app/api/nowpayments.py",
    "app/api/operator.py",
    "app/api/smart_folders.py",
    "app/api/style_profiles.py",
    "app/api/team.py",
    "app/api/telegram_verify.py",
    "app/api/webhook_settings.py",
    # === app/crud/ ===
    "app/crud/account.py",
    "app/crud/broadcast.py",
    "app/crud/recurring_schedule.py",
    "app/crud/reply_macro.py",
    # === app/core/ ===
    "app/core/crypto.py",
    # === app/bot/ ===
    "app/bot/service.py",
    "app/bot/ai_employee.py",
    "app/bot/guest_engine.py",
    "app/bot/ephemeral_engine.py",
    "app/bot/db.py",
    # === app/routers/ ===
    "app/routers/draft_routes.py",
    "app/routers/trigger_routes.py",
    # === app/scheduler/ ===
    "app/scheduler/scheduler.py",
    # === app/models/ ===
    "app/models/tenant.py",
    "app/models/broadcast.py",
    "app/models/guide_hub.py",
    "app/models/token.py",
    "app/models/message_template.py",
    # === etc ===
    "app/config.py",
    "app/routes/miniapp_routes.py",
    "app/schemas/broadcast.py",
    "app/schemas/content_studio.py",
    "app/schemas/reply_macro.py",
    "app/schemas/team.py",
    "app/schemas/style_profile.py",
    "app/schemas/join_queue.py",
    # === app/services/ (AI chat v2) ===
    "app/services/ai_chat_v2_service.py",
    "app/services/ai_reply_v2_service.py",
    "app/services/delivery_analytics.py",
    "app/services/telegram_membership.py",
    "app/services/telegram_notify.py",
    "app/services/cryptomus.py",
    "app/services/telethon_pool.py",
]


def _strip_non_ascii_bytes(data: bytes) -> bytes:
    """Return only ASCII bytes from a byte string."""
    return bytes(b for b in data if b < 128)


def _normalize_line_endings(data: bytes) -> bytes:
    """Normalize \\r\\r\\n and \\r\\n to \\n."""
    return data.replace(b'\r\r\n', b'\n').replace(b'\r\n', b'\n')


def _has_non_ascii(data: bytes) -> bool:
    """Check if bytes contain any non-ASCII byte (>127)."""
    return any(b > 127 for b in data)


def restore_file_byte_level(filepath: str, repo_root: str) -> bool:
    """Restore Korean strings using byte-level comparison."""
    full_path = os.path.join(repo_root, filepath)

    if not os.path.exists(full_path):
        print(f"  MISSING: {filepath}")
        return False

    # Get original from git
    try:
        result = subprocess.run(
            ['git', 'show', f'HEAD:{filepath}'],
            capture_output=True,
            timeout=30
        )
        if result.returncode != 0:
            print(f"  NO GIT: {filepath}")
            return False
        orig_bytes = result.stdout
    except Exception as e:
        print(f"  ERROR reading git: {filepath} - {e}")
        return False

    # Check if original has any non-ASCII
    if not _has_non_ascii(orig_bytes):
        print(f"  SKIP (no non-ASCII): {filepath}")
        return False

    # Normalize original line endings
    orig_norm = _normalize_line_endings(orig_bytes)
    orig_lines = orig_norm.split(b'\n')

    # Read current file
    with open(full_path, 'rb') as f:
        curr_bytes = f.read()

    curr_norm = _normalize_line_endings(curr_bytes)
    curr_lines = curr_norm.split(b'\n')

    new_lines = list(curr_lines)
    restorations = 0

    # For each original line with non-ASCII, try to find match in current
    for oi, oline in enumerate(orig_lines):
        if not _has_non_ascii(oline):
            continue

        # Create ASCII-only version of this original line
        stripped = _strip_non_ascii_bytes(oline)

        # Skip very short matches (avoid false positives)
        if len(stripped) < 5:
            continue

        # Remove trailing whitespace for comparison
        stripped_clean = stripped.rstrip()

        # Try to find matching line in current (search forward from last match)
        found = False
        for ci in range(len(new_lines)):
            curr_line = new_lines[ci].rstrip()

            # Compare: stripped original vs current line
            if stripped_clean == curr_line or stripped_clean == curr_line + b'?' * (len(stripped_clean) - len(curr_line)):
                new_lines[ci] = oline
                restorations += 1
                found = True
                break

            # Also try fuzzy match for longer lines
            if len(stripped_clean) > 30 and not found:
                # Check if current line matches a prefix/suffix of stripped
                prefix = stripped_clean[:30]
                if prefix == curr_line[:len(prefix)] and abs(len(stripped_clean) - len(curr_line)) > 10:
                    new_lines[ci] = oline
                    restorations += 1
                    found = True
                    break

    if restorations > 0:
        new_content = b'\n'.join(new_lines)
        with open(full_path, 'wb') as f:
            f.write(new_content)
        print(f"  RESTORED {restorations} strings: {filepath}")
        return True
    else:
        print(f"  NO MATCHES: {filepath}")
        return False


def main():
    repo_root = os.getcwd()
    print(f"Repository root: {repo_root}")
    print(f"Processing {len(CRITICAL_FILES)} critical files...\n")

    restored = 0
    skipped = 0
    no_match = 0
    missing = 0

    for filepath in CRITICAL_FILES:
        result = restore_file_byte_level(filepath, repo_root)
        if result is True:
            restored += 1
        elif result is None:
            skipped += 1
        elif result is False:
            no_match += 1

    print(f"\n=== SUMMARY ===")
    print(f"  Restored: {restored}")
    print(f"  No matches: {no_match}")
    print(f"  Skipped: {skipped}")
    print(f"  Missing: {missing}")


if __name__ == '__main__':
    main()
