"""
Restore Korean strings from git history into current cleaned files.

For each critical file:
  1. Read original from git (binary, preserve Korean chars)
  2. Read current cleaned file (ASCII only)
  3. For each original line that had Korean chars, find the matching line
     in the current file and restore the Korean chars
  4. Write the restored file back (UTF-8)
"""

import subprocess
import os
import re
import sys

# Critical files that need Korean string restoration
CRITICAL_FILES = [
    # === AI PROMPTS (app/services/) ===
    "app/services/ai_chatbot_service.py",
    "app/services/ai_chat_service.py",
    "app/services/ai_chat_v2_service.py",
    "app/services/ai_analysis_service.py",
    "app/services/ai_content_studio_service.py",
    "app/services/ai_core_service.py",
    "app/services/ai_growth_coach_service.py",
    "app/services/ai_ops_service.py",
    "app/services/ai_reply_service.py",
    "app/services/ai_reply_v2_service.py",
    "app/services/ai_spam_guard_service.py",
    "app/services/ai_style_service.py",
    "app/services/bot_ai_agent_service.py",
    "app/services/billing.py",
    "app/services/chat_service.py",
    "app/services/deepseek_service.py",
    "app/services/delivery.py",
    "app/services/delivery_analytics.py",
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
    "app/services/broadcast_distribution.py",
    "app/services/broadcast_processor.py",
    "app/services/webhook_service.py",
    "app/services/account_health.py",
    "app/services/failure_intel.py",
    "app/services/group_search_service.py",
    "app/services/join_queue_service.py",
    "app/services/link_inspector_service.py",
    "app/services/telegram_bot_service.py",
    "app/services/telegram_actions.py",
    "app/services/media.py",
    "app/services/sms_service.py",
    "app/services/telegram_membership.py",
    "app/services/telegram_notify.py",
    "app/services/telethon_pool.py",
    "app/services/bot_account_service.py",
    "app/services/bot_api_key_service.py",
    "app/services/cryptomus.py",
    "app/services/fortune_service.py",
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
    "app/api/fortune.py",
    "app/api/ai_agent.py",
    "app/api/ai_assist.py",
    "app/api/ai_chat_v2.py",
    "app/api/ai_reply_v2.py",
    "app/api/runtime.py",
    "app/api/features.py",
    "app/api/mcp_gateway.py",
    "app/api/delivery_analytics.py",
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
    "app/api/push_notifications.py",
    "app/api/smart_folders.py",
    "app/api/style_profiles.py",
    "app/api/team.py",
    "app/api/telegram_verify.py",
    "app/api/webhook_settings.py",
    # === app/crud/ ===
    "app/crud/account.py",
    "app/crud/broadcast.py",
    "app/crud/join_queue.py",
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
    "app/bot/telegram_api.py",
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
    "app/database.py",
    "app/main.py",
    "app/cache.py",
    "app/monitoring.py",
    "app/admin_platform.py",
    "app/routes/miniapp_routes.py",
    "app/schemas/broadcast.py",
    "app/schemas/content_studio.py",
    "app/schemas/reply_macro.py",
    "app/schemas/team.py",
    "app/schemas/style_profile.py",
    "app/schemas/join_queue.py",
]


def has_korean(text: str) -> bool:
    """Check if text contains Korean characters (Hangul)."""
    for ch in text:
        cp = ord(ch)
        if (0xAC00 <= cp <= 0xD7AF) or \
           (0x1100 <= cp <= 0x11FF) or \
           (0x3130 <= cp <= 0x318F) or \
           (0xA960 <= cp <= 0xA97C) or \
           (0xD7B0 <= cp <= 0xD7FF):
            return True
    return False


def get_original_from_git(filepath: str) -> str | None:
    """Get the original file content from git HEAD."""
    try:
        result = subprocess.run(
            ['git', 'show', f'HEAD:{filepath}'],
            capture_output=True,
            timeout=30
        )
        if result.returncode == 0:
            return result.stdout.decode('utf-8', errors='replace')
        return None
    except Exception as e:
        print(f"  Error getting git content for {filepath}: {e}")
        return None


def restore_file(filepath: str, repo_root: str) -> bool:
    """Restore Korean strings in a file from its git original."""
    full_path = os.path.join(repo_root, filepath)
    
    if not os.path.exists(full_path):
        print(f"  SKIP: {filepath} does not exist")
        return False
    
    original = get_original_from_git(filepath)
    if original is None:
        print(f"  SKIP: {filepath} - no git history")
        return False
    
    # Check if original had Korean
    if not has_korean(original):
        print(f"  SKIP: {filepath} - no Korean in original")
        return False
    
    # Read current file
    with open(full_path, 'rb') as f:
        current_bytes = f.read()
    
    current = current_bytes.decode('utf-8', errors='replace')
    
    # Split into lines (normalize line endings)
    orig_lines = original.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    curr_lines = current.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    
    restorations = 0
    new_lines = list(curr_lines)
    
    # For each original line with Korean, try to find its match in current
    orig_idx = 0
    curr_idx = 0
    
    while orig_idx < len(orig_lines):
        orig_line = orig_lines[orig_idx]
        orig_idx += 1
        
        if not has_korean(orig_line):
            # No Korean to restore on this line
            continue
        
        # Create "stripped" version of the original line (remove Korean chars)
        stripped = ''
        for ch in orig_line:
            if ord(ch) < 128 and ch not in '\r':
                stripped += ch
            elif ch in '\t ':
                stripped += ch
        
        # Remove trailing whitespace from stripped
        stripped = stripped.rstrip()
        
        # Find this line in the current file
        found = False
        for ci in range(curr_idx, min(curr_idx + 5, len(curr_lines))):
            curr_stripped = curr_lines[ci].rstrip()
            # Compare stripped versions
            if stripped == curr_stripped:
                # Found it! Restore the original line with Korean
                new_lines[ci] = orig_line
                restorations += 1
                curr_idx = ci + 1
                found = True
                break
            # Also try matching just the beginning (for long lines)
            min_len = min(len(stripped), 20)
            if min_len > 10 and stripped[:min_len] == curr_stripped[:min_len] and abs(len(stripped) - len(curr_stripped)) > 10:
                new_lines[ci] = orig_line
                restorations += 1
                curr_idx = ci + 1
                found = True
                break
        
        if not found:
            # Try searching more broadly
            for ci in range(len(curr_lines)):
                curr_stripped = curr_lines[ci].rstrip()
                if stripped == curr_stripped:
                    new_lines[ci] = orig_line
                    restorations += 1
                    break
    
    if restorations > 0:
        # Write the restored file
        new_content = '\n'.join(new_lines)
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f"  RESTORED {restorations} strings in {filepath}")
        return True
    else:
        print(f"  NO MATCH: {filepath} - had Korean but couldn't find matching lines")
        return False


def main():
    repo_root = os.getcwd()
    print(f"Repository root: {repo_root}")
    print(f"Restoring {len(CRITICAL_FILES)} critical files...\n")
    
    success = 0
    skipped = 0
    no_match = 0
    
    for filepath in CRITICAL_FILES:
        result = restore_file(filepath, repo_root)
        if result:
            success += 1
        elif result is None:
            skipped += 1
        else:
            no_match += 1
    
    print(f"\n=== SUMMARY ===")
    print(f"Restored: {success}")
    print(f"Skipped (no Korean or no git history): {skipped}")
    print(f"No match found: {no_match}")


if __name__ == '__main__':
    main()
