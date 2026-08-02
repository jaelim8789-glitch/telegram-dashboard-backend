import os
os.environ.setdefault('ENVIRONMENT', 'development')
os.environ['DEBUG'] = 'true'
os.environ['SMS_PROVIDER'] = 'console'
os.environ.setdefault('DATABASE_URL', 'sqlite+aiosqlite:///:memory:')
os.environ.setdefault('ENCRYPTION_KEY', 'I62a0BiduGAdZjg9UH_vg3VuIEQMpe2AyDm2DfM2HlA=')
os.environ.setdefault('TELEGRAM_API_ID', '12345')
os.environ.setdefault('TELEGRAM_API_HASH', 'test_hash_abcdef')
os.environ['TELEGRAM_BOT_TOKEN'] = ''
os.environ['TELEGRAM_OFFICIAL_CHANNEL_ID'] = ''

import app.main as main_module
print('module file:', main_module.__file__)
print('app type:', type(main_module.app))
print('app module:', getattr(main_module.app, '__module__', None))
print('FastAPI symbol type:', type(main_module.FastAPI))
print('FastAPI symbol module:', getattr(main_module.FastAPI, '__module__', None))
print('FastAPI repr:', main_module.FastAPI)
