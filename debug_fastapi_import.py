import os
os.environ['ENVIRONMENT'] = 'development'
os.environ['DEBUG'] = 'true'
os.environ['SMS_PROVIDER'] = 'console'
os.environ['DATABASE_URL'] = 'sqlite+aiosqlite:///:memory:'
os.environ['ENCRYPTION_KEY'] = 'I62a0BiduGAdZjg9UH_vg3VuIEQMpe2AyDm2DfM2HlA='
os.environ['TELEGRAM_API_ID'] = '12345'
os.environ['TELEGRAM_API_HASH'] = 'test_hash_abcdef'
os.environ['TELEGRAM_BOT_TOKEN'] = ''
os.environ['TELEGRAM_OFFICIAL_CHANNEL_ID'] = ''
import app.main as app_main_module
from fastapi import FastAPI as fastapi_class
print('module_FastAPI_type=', type(app_main_module.FastAPI))
print('module_FastAPI_module=', getattr(app_main_module.FastAPI, '__module__', None))
print('module_FastAPI_repr=', app_main_module.FastAPI)
print('imported_FastAPI_type=', type(fastapi_class))
print('imported_FastAPI_module=', getattr(fastapi_class, '__module__', None))
print('module_app_type=', type(app_main_module.app))
print('module_app_repr=', app_main_module.app)
