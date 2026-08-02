import app.api.chats as chats
import app.main as main
print('chats_file', chats.__file__)
print('chat_routes', [getattr(r, 'path', '') for r in chats.router.routes])
print('main_routes', [getattr(r, 'path', '') for r in main.app.router.routes if 'chat-telegram' in getattr(r, 'path', '') or 'upload' in getattr(r, 'path', '')])
