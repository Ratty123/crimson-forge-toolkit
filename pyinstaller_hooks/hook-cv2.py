import glob
import os
import pathlib
import sys

import PyInstaller.utils.hooks as hookutils
from PyInstaller import compat


hiddenimports = ["numpy"]
binaries = []


def _should_keep_binary(src_path: str) -> bool:
    name = os.path.basename(src_path).lower()
    # The app uses OpenCV only for image and geometry operations, not video IO.
    # Excluding the FFmpeg plugin avoids onefile extraction failures on some systems.
    return not name.startswith("opencv_videoio_ffmpeg")


if compat.is_win:
    if compat.is_conda:
        libdir = os.path.join(compat.base_prefix, "Library", "bin")
        pattern = os.path.join(libdir, "opencv_videoio_ffmpeg*.dll")
        for src_path in glob.glob(pattern):
            if _should_keep_binary(src_path):
                binaries.append((src_path, "."))

    binaries += [
        (src_path, dest_path)
        for src_path, dest_path in hookutils.collect_dynamic_libs("cv2")
        if _should_keep_binary(src_path)
    ]


hiddenimports += hookutils.collect_submodules("cv2", filter=lambda name: name != "cv2.load_config_py2")
excludedimports = ["cv2.load_config_py2"]

datas = hookutils.collect_data_files(
    "cv2",
    include_py_files=True,
    includes=[
        "config.py",
        f"config-{sys.version_info[0]}.{sys.version_info[1]}.py",
        "config-3.py",
        "load_config_py3.py",
    ],
)


def find_cv2_extension(config_file: str):
    PYTHON_EXTENSIONS_PATHS = []
    LOADER_DIR = os.path.dirname(os.path.abspath(os.path.realpath(config_file)))

    global_vars = globals().copy()
    local_vars = locals().copy()

    with open(config_file, encoding="utf-8") as handle:
        code = compile(handle.read(), os.path.basename(config_file), "exec")
    exec(code, global_vars, local_vars)

    PYTHON_EXTENSIONS_PATHS = local_vars["PYTHON_EXTENSIONS_PATHS"]
    if not PYTHON_EXTENSIONS_PATHS:
        return None

    for extension_path in PYTHON_EXTENSIONS_PATHS:
        extension_path = pathlib.Path(extension_path)
        extension_files = list(extension_path.glob("cv2*.pyd" if compat.is_win else "cv2*.so"))
        if extension_files:
            extension_file = extension_files[0]
            dest_dir = pathlib.Path("cv2") / extension_file.parent.relative_to(LOADER_DIR)
            return str(extension_file), str(dest_dir)

    hookutils.logger.warning(
        "Could not find cv2 extension module! Config file: %s, search paths: %s",
        config_file,
        PYTHON_EXTENSIONS_PATHS,
    )
    return None


config_file = [
    src_path
    for src_path, _dest_path in datas
    if os.path.basename(src_path) in (f"config-{sys.version_info[0]}.{sys.version_info[1]}.py", "config-3.py")
]

if config_file:
    try:
        extension_info = find_cv2_extension(config_file[0])
        if extension_info:
            ext_src, ext_dst = extension_info
            if ext_dst == "cv2":
                hiddenimports += ["cv2.cv2"]
            else:
                binaries += [extension_info]
    except Exception:
        hookutils.logger.warning("Failed to determine location of cv2 extension module!", exc_info=True)


module_collection_mode = "py"


if compat.is_linux:
    pkg_path = pathlib.Path(hookutils.get_module_file_attribute("cv2")).parent
    qt_fonts_dir = pkg_path / "qt" / "fonts"
    datas += [
        (str(font_file), str(font_file.parent.relative_to(pkg_path.parent)))
        for font_file in qt_fonts_dir.rglob("*.ttf")
    ]
    qt_plugins_dir = pkg_path / "qt" / "plugins"
    binaries += [
        (str(plugin_file), str(plugin_file.parent.relative_to(pkg_path.parent)))
        for plugin_file in qt_plugins_dir.rglob("*.so")
    ]
