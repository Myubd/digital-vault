# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import copy_metadata, collect_all

datas_meta = []
for pkg in [
    'fastapi', 'uvicorn', 'starlette', 'pydantic', 'anyio', 'httpx', 'cryptography',
]:
    try:
        datas_meta += copy_metadata(pkg)
    except Exception:
        pass

# archlife-fastapi/launch_fastapi.spec と同じ理由:
# local_ai_core が依存する外部パッケージ(httpx, cryptography)は
# collect_all('local_ai_core') だけでは検出されないため、明示的にcollect_allする。
local_ai_core_datas, local_ai_core_binaries, local_ai_core_hiddenimports = collect_all('local_ai_core')
httpx_datas, httpx_binaries, httpx_hiddenimports = collect_all('httpx')
cryptography_datas, cryptography_binaries, cryptography_hiddenimports = collect_all('cryptography')

a = Analysis(
    ['run_service.py'],
    pathex=[],
    binaries=local_ai_core_binaries + httpx_binaries + cryptography_binaries,
    datas=datas_meta + local_ai_core_datas + httpx_datas + cryptography_datas + [
        ('main.py', '.'),
        ('service_auth.py', '.'),
        # plugin_manifest.json: core_sync/bootstrap.py がファイルパスで直接
        # 読むため、PyInstallerの自動import解析だけでは拾われない
        # (archlife-fastapi/health-supportで実際に踏んだ問題を踏まえ、
        # 最初から入れておく)。
        ('plugin_manifest.json', '.'),
        # static/index.html: main.pyの "/" ルートがファイルパスで直接読むため、
        # PyInstallerの自動import解析だけでは拾われない
        # (health-support/study-supportで実際に踏んだ問題と同じ)。
        ('static', 'static'),
    ],
    hiddenimports=[
        'uvicorn', 'uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto',
        'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets', 'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan', 'uvicorn.lifespan.on',
        'fastapi', 'starlette',
        'anyio', 'anyio._backends._asyncio',
        'main', 'service_auth',
    ] + local_ai_core_hiddenimports + httpx_hiddenimports + cryptography_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='digital_vault',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='digital_vault',
)
