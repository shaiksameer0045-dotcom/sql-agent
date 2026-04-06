# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for QueryLux server
# Run: pyinstaller server.spec --clean --noconfirm

import sys
from pathlib import Path

block_cipher = None

a = Analysis(
    ['server_entry.py'],
    pathex=[str(Path('.'))]  ,
    binaries=[],
    datas=[
        # Bundle static web assets into the binary
        ('static', 'static'),
    ],
    hiddenimports=[
        # uvicorn internals
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.loops.asyncio',
        'uvicorn.loops.uvloop',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.http.httptools_impl',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.protocols.websockets.websockets_impl',
        'uvicorn.protocols.websockets.wsproto_impl',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'uvicorn.lifespan.off',
        # FastAPI / starlette
        'starlette.routing',
        'starlette.middleware',
        'starlette.middleware.cors',
        # Pydantic
        'pydantic.deprecated.class_validators',
        'pydantic_core',
        # DB drivers
        'duckdb',
        'psycopg2',
        'psycopg2.extensions',
        'pymysql',
        'pymysql.converters',
        # Crypto
        'cryptography',
        'cryptography.fernet',
        'cryptography.hazmat.primitives.ciphers.aead',
        # SSH
        'paramiko',
        'sshtunnel',
        # Firebase
        'firebase_admin',
        'firebase_admin.auth',
        'firebase_admin._auth_utils',
        'google.auth',
        'google.auth.crypt',
        'google.auth.crypt._python_rsa',
        'google.auth.transport',
        'google.auth.transport.requests',
        'google.oauth2',
        'google.oauth2.id_token',
        # Groq
        'groq',
        'groq._streaming',
        'groq.types',
        # APScheduler
        'apscheduler',
        'apscheduler.schedulers.background',
        'apscheduler.executors.pool',
        # Email (used internally by some deps)
        'email.mime.text',
        'email.mime.multipart',
        # Encoding
        'encodings.utf_8',
        'encodings.ascii',
        'encodings.idna',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'matplotlib', 'numpy', 'pandas',
        'PIL', 'PyQt5', 'wx', 'gi',
        'IPython', 'notebook', 'pytest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,    # one-dir mode (faster startup)
    name='server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,            # no console window on Windows
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='server',
)
