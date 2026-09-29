# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

version_namespace = {}
exec(Path('src/version.py').read_text(encoding='utf-8'), version_namespace)
EXE_NAME = version_namespace['EXE_NAME']

a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('src/templates', 'src/templates'),
        ('static', 'static'),
        ('frontend/dist', 'frontend/dist'),
    ],
    hiddenimports=collect_submodules('gallery_dl') + [
        'jinja2.ext',
        'PIL',
        'PIL.Image',
        'pystray',
        'pystray._win32',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
