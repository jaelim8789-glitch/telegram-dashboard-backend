"""
Telegram Bot Module — Bot API integration, fully separate from the
Telethon-based account runtime (backend/account_runtime.py).

Responsibilities:
  - Guide new users through signup / free trial
  - Verify channel subscription
  - Bridge into the existing free-API-key issuance flow
  - Notify admins of key lifecycle events

This package never imports Telethon and never touches accounts,
broadcasts, auto-reply, reply-macro, or scheduler code.
"""
