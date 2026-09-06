# -*- mode: python ; coding: utf-8 -*-

# Bundle the prompt_toolkit + Rich TUI (tui.py + rich + prompt_toolkit) into the onefile exe.
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = ['tui']

for pkg in ('rich', 'prompt_toolkit'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h


a = Analysis(
    ['agent.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'pydoc'],
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='deepseek-agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
