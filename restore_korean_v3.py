"""
Index-based Korean string restoration from git history.

Strategy:
  1. Read original from git as bytes
  2. Read current file as bytes
  3. Normalize line endings in both: \\r\\r\\n -> \\n, \\r\\n -> \\n
  4. Split both into lines by \\n
  5. Verify line counts match
  6. For each index i:
     a. If orig_lines[i] has non-ASCII (Korean)
     b. Create ASCII-only version of orig_lines[i]
     c. Verify it matches curr_lines[i] (accounting for trailing whitespace/\\r)
     d. If match, replace curr_lines[i] with orig_lines[i]
  7. Join and write back
"""

import subprocess
import os
import glob

# Files to EXCLUDE from restoration (manually edited)
EXCLUDE_FILES = {
    'app/services/fortune_service.py',
    'app/api/fortune.py',
}

CRITICAL_FILES = [
    # === AI PROMPTS ===
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
    "app/services/ai_chat_v2_service.py",
    "app/services/ai_reply_v2_service.py",
    "app/services/delivery_analytics.py",
    "app/services/telegram_membership.py",
    "app/services/telegram_notify.py",
    "app/services/cryptomus.py",
    "app/services/telethon_pool.py",
    # === USER MESSAGES ===
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
    # === CRUD ===
    "app/crud/account.py",
    "app/crud/broadcast.py",
    "app/crud/recurring_schedule.py",
    "app/crud/reply_macro.py",
    # === CORE ===
    "app/core/crypto.py",
    # === BOT ===
    "app/bot/service.py",
    "app/bot/ai_employee.py",
    "app/bot/guest_engine.py",
    "app/bot/ephemeral_engine.py",
    "app/bot/db.py",
    # === ROUTERS ===
    "app/routers/draft_routes.py",
    "app/routers/trigger_routes.py",
    # === SCHEDULER ===
    "app/scheduler/scheduler.py",
    # === MODELS ===
    "app/models/tenant.py",
    "app/models/broadcast.py",
    "app/models/guide_hub.py",
    "app/models/token.py",
    "app/models/message_template.py",
    # === ETC ===
    "app/config.py",
    "app/routes/miniapp_routes.py",
    "app/schemas/broadcast.py",
    "app/schemas/content_studio.py",
    "app/schemas/reply_macro.py",
    "app/schemas/team.py",
    "app/schemas/style_profile.py",
    "app/schemas/join_queue.py",
]


def norm_line_endings(data: bytes) -> bytes:
    return data.replace(b'\r\r\n', b'\n').replace(b'\r\n', b'\n')


def is_ascii(data: bytes) -> bool:
    return all(b < 128 for b in data)


def strip_non_ascii(data: bytes) -> bytes:
    return bytes(b for b in data if b < 128)


def count_korean(data: bytes) -> int:
    """Count how many bytes could be part of Korean UTF-8 sequences."""
    try:
        text = data.decode('utf-8', errors='replace')
        return sum(1 for c in text if 0xAC00 <= ord(c) <= 0xD7AF)
    except:
        return 0


def main():
    repo_root = os.getcwd()
    print(f"Processing {len(CRITICAL_FILES)} files...\n")

    total_restored = 0
    total_mismatch = 0
    total_skipped = 0

    for filepath in CRITICAL_FILES:
        if filepath in EXCLUDE_FILES:
            print(f"  SKIP (excluded): {filepath}")
            total_skipped += 1
            continue

        full_path = os.path.join(repo_root, filepath)
        if not os.path.exists(full_path):
            print(f"  MISSING: {filepath}")
            continue

        # Get original from git
        result = subprocess.run(
            ['git', 'show', f'HEAD:{filepath}'],
            capture_output=True, timeout=15
        )
        if result.returncode != 0:
            print(f"  NO GIT: {filepath}")
            continue

        orig_bytes = result.stdout

        # Check if original has any Korean
        if count_korean(orig_bytes) == 0:
            print(f"  SKIP (no Korean): {filepath}")
            total_skipped += 1
            continue

        # Normalize line endings
        orig_norm = norm_line_endings(orig_bytes)
        orig_lines = orig_norm.split(b'\n')

        # Read current file
        with open(full_path, 'rb') as f:
            curr_bytes = f.read()
        curr_norm = norm_line_endings(curr_bytes)
        curr_lines = curr_norm.split(b'\n')

        # Verify line counts match
        if len(orig_lines) != len(curr_lines):
            print(f"  LINE COUNT MISMATCH ({len(orig_lines)} vs {len(curr_lines)}): {filepath}")
            total_mismatch += 1
            continue

        # Index-based restoration
        restored = 0
        mismatches = 0
        new_lines = list(curr_lines)

        for i in range(len(orig_lines)):
            oline = orig_lines[i]
            if not any(b > 127 for b in oline):
                continue  # No non-ASCII in this line

            # Compare stripped version
            stripped = strip_non_ascii(oline).rstrip()
            curr = curr_lines[i].rstrip()

            if stripped == curr:
                # Perfect match - restore the original line with Korean
                new_lines[i] = oline
                restored += 1
            elif stripped.rstrip(b'\r') == curr:
                new_lines[i] = oline
                restored += 1
            else:
                mismatches += 1

        if restored > 0:
            new_content = b'\n'.join(new_lines)
            with open(full_path, 'wb') as f:
                f.write(new_content)
            print(f"  RESTORED {restored} lines ({mismatches} skipped): {filepath}")
            total_restored += 1
        else:
            print(f"  NO RESTORATIONS: {filepath}")
            total_skipped += 1

    print(f"\n=== SUMMARY ===")
    print(f"  Files restored: {total_restored}")
    print(f"  Line count mismatches: {total_mismatch}")
    print(f"  Skipped: {total_skipped}")


if __name__ == '__main__':
    main()
