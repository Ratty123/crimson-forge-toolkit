"""
Keep Pillow on PyInstaller's standard collection path.

The previous hook used collect_all("PIL"), which can produce corrupt onefile
payloads for compiled Pillow modules like ``_imagingft``. This hook now only
applies lightweight import exclusions and lets PyInstaller's built-in handling
bundle Pillow normally.
"""

# Mirror the intent of PyInstaller's built-in Pillow hook while also excluding
# AVIF support that we do not ship.
excludedimports = [
    "tkinter",
    "PyQt5",
    "PySide2",
    "PyQt6",
    "PySide6",
    "IPython",
    "PIL.AvifImagePlugin",
    "PIL._avif",
]
