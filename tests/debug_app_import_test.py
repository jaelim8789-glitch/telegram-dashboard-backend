import app.main as app_main_module
from fastapi import FastAPI as fastapi_class


def test_debug_app_type():
    print('module_file=', app_main_module.__file__)
    print('module_FastAPI_type=', type(app_main_module.FastAPI))
    print('module_FastAPI_repr=', app_main_module.FastAPI)
    print('module_FastAPI_module=', getattr(app_main_module.FastAPI, '__module__', None))
    print('module_FastAPI_mro=', getattr(app_main_module.FastAPI, '__mro__', None))
    print('imported_FastAPI_type=', type(fastapi_class))
    print('imported_FastAPI_repr=', fastapi_class)
    print('module_app_type=', type(app_main_module.app))
    print('module_app_repr=', app_main_module.app)
    print('module_app_has_dependency_overrides=', hasattr(app_main_module.app, 'dependency_overrides'))
