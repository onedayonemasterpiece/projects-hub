# Telegram routing

Owner-confirmed on 2026-10-05.

| Project | Chat | Topic | Link | Telegram chat ID | Topic ID |
| --- | --- | --- | --- | --- | --- |
| Projects Hub | Мои приложения | Projects-hub | https://t.me/c/4488229487/29 | `-1004488229487` | `29` |

When the owner asks to send a Projects Hub login code or a message to the application's chat, use this topic. For a Telegram forum message, set `message_thread_id=29` (Bot API) or `reply_to=29` (Telethon). Verify delivery in this topic without recording the login code in logs or documentation.

This routing records the destination; sending still requires the owner's instruction and an authorized Telegram identity. It is separate from in-product Live project routing.
