"""
Setup script for SiteDownloader macOS app.
Build:  python setup.py py2app
"""
import sys
import os
from setuptools import setup

APP_NAME = "SiteDownloader"

APP = ["site_downloader_gui.py"]

DATA_FILES = [
    ("", ["site_downloader.py"]),
    ("", ["_playwright_runner.py"]),
    ("", ["requirements.txt"]),
    ("", ["README.md"]),
    ("mirror", [
        "mirror/mirror_site_sequential.py",
        "mirror/mirror_site_parallel.py",
        "mirror/mirror_full.py",
        "mirror/download_page.py",
        "mirror/download_missing.py",
    ]),
    ("utils", [
        "utils/fix_paths.py",
        "utils/fix_paths_absolute.py",
        "utils/fix_links.py",
        "utils/audit_result.py",
        "utils/diagnose_links.py",
        "utils/probe_missing.py",
        "utils/check_links.py",
    ]),
    ("examples", [
        "examples/fetch_media.py",
        "examples/migrate.py",
        "examples/build_site.py",
    ]),
]

OPTIONS = {
    "argv_emulation": False,
    "includes": [
        "tkinter",
        "requests",
        "bs4",
        "json",
        "re",
        "threading",
        "queue",
        "time",
        "urllib.parse",
        "urllib.error",
        "urllib.request",
        "collections",
        "concurrent.futures",
        "dataclasses",
        "pathlib",
        "io",
        "traceback",
        "ssl",
    ],
    "excludes": [
        "PyQt5", "PyQt6", "PySide2", "PySide6",
        "matplotlib", "numpy", "pandas",
        "PIL", "Pillow",
        "notebook", "jupyter",
        "tensorflow", "torch",
        "cv2", "opencv",
    ],
    "packages": [
        "requests",
        "bs4",
        "urllib3",
        "charset_normalizer",
        "idna",
        "certifi",
    ],
    "iconfile": "icon.icns" if os.path.exists("icon.icns") else None,
    "plist": {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": "com.sitedownloader.app",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleExecutable": APP_NAME,
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
        "LSMinimumSystemVersion": "11.0",
        "NSHumanReadableCopyright": "© 2025 SiteDownloader",
    },
}

setup(
    name=APP_NAME,
    version="1.0.0",
    description="SiteDownloader — Download websites for offline viewing",
    author="SiteDownloader",
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
