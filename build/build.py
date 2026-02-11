#!/usr/bin/env python3
"""
Скрипт сборки 1CParserPFF в dist.

Создает единый самодостаточный exe-файл: dist/1CParserPFF.exe (GUI).

Использование:
    python build/build.py

Требует: pip install pyinstaller
"""

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
DIST = os.path.join(ROOT, "dist")
BUILD = os.path.join(ROOT, "build")


def main():
    # Очистить dist
    if os.path.exists(DIST):
        shutil.rmtree(DIST)
    os.makedirs(DIST)

    # Проверить PyInstaller
    try:
        import PyInstaller
    except ImportError:
        print("Установите PyInstaller: pip install pyinstaller")
        sys.exit(1)

    # Общие опции
    # --distpath: куда класть exe
    # --workpath: куда класть временные файлы сборки
    # --specpath: куда класть spec-файлы
    # --clean: очистить кэш PyInstaller
    # --noconfirm: не спрашивать подтверждение перезаписи
    common_opts = [
        "--distpath", DIST,
        "--workpath", BUILD,
        "--specpath", BUILD,
        "--clean",
        "--noconfirm",
    ]

    print("Сборка 1CParserPFF.exe (GUI)...")
    
    # Сборка GUI в один файл (onefile) без консоли (noconsole)
    gui_script = os.path.join(SRC, "pff_parser_gui.py")
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller",
            *common_opts,
            "--onefile",
            "--noconsole",
            "--name", "1CParserPFF",
            gui_script
        ],
        check=True,
        cwd=ROOT,
    )

    print(f"\nСборка завершена. Файл: {os.path.join(DIST, '1CParserPFF.exe')}")


if __name__ == "__main__":
    main()
