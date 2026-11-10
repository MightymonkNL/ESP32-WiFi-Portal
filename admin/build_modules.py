"""Builds the firmware of every module and copies it into admin/modules/<id>/firmware/.

Also copies esptool.exe (from PlatformIO) into admin/tools, so the Toolbox can flash.
PlatformIO is found as 'pio' / 'platformio', in the VS Code extension's folder, or as a Python
module; when it is missing it is installed with pip (the first build then downloads the ESP32
toolchain, about 1 GB, which takes a while).
Usage: python admin/build_modules.py [module-id ...]
"""

import glob
import importlib.util
import json
import os
import shutil
import subprocess
import sys

ADMIN = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(ADMIN)
PIO_HOME = os.environ.get("PLATFORMIO_CORE_DIR") or os.path.join(os.path.expanduser("~"), ".platformio")
_PIO = None


def pio():
    """The command to run PlatformIO (a list), installing it with pip when it is missing."""
    global _PIO
    if _PIO:
        return _PIO
    found = shutil.which("pio") or shutil.which("platformio")
    if found:
        _PIO = [found]
        return _PIO
    for exe in (os.path.join(PIO_HOME, "penv", "Scripts", "pio.exe"),      # VS Code extension (Windows)
                os.path.join(PIO_HOME, "penv", "bin", "pio")):
        if os.path.exists(exe):
            _PIO = [exe]
            return _PIO
    if importlib.util.find_spec("platformio") is None:
        print("PlatformIO not found: installing it with pip (once)...")
        subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--quiet", "platformio"], check=True)
        importlib.invalidate_caches()
    _PIO = [sys.executable, "-m", "platformio"]
    try:
        subprocess.run(_PIO + ["--version"], check=True)
    except (OSError, subprocess.CalledProcessError):
        sys.exit("PlatformIO could not be started. Install it: python -m pip install --user platformio")
    return _PIO


def build(meta, folder):
    project = os.path.normpath(os.path.join(ROOT, meta["project"]))
    print(f"\n=== {meta['name']} ({meta['id']}) - {project}")
    subprocess.run(pio() + ["run", "-d", project], check=True)
    builds = sorted(glob.glob(os.path.join(project, ".pio", "build", "*", "firmware.bin")),
                    key=os.path.getmtime)
    if not builds:
        sys.exit(f"No firmware.bin found in {project}\\.pio\\build")
    build_dir = os.path.dirname(builds[-1])
    boot_app0 = os.path.join(PIO_HOME, "packages", "framework-arduinoespressif32", "tools", "partitions",
                             "boot_app0.bin")
    sources = {"bootloader.bin": os.path.join(build_dir, "bootloader.bin"),
               "partitions.bin": os.path.join(build_dir, "partitions.bin"),
               "firmware.bin": os.path.join(build_dir, "firmware.bin"),
               "boot_app0.bin": boot_app0}
    out = os.path.join(folder, "firmware")
    os.makedirs(out, exist_ok=True)
    for name in meta.get("flash", {}).values():
        if name not in sources or not os.path.exists(sources[name]):
            sys.exit(f"{name} not found for {meta['id']}")
        shutil.copyfile(sources[name], os.path.join(out, name))
    print(f"--> {out}")


def copy_esptool():
    """esptool.exe from PlatformIO into admin/tools (installs PlatformIO's esptool package if needed)."""
    tool = os.path.join(PIO_HOME, "packages", "tool-esptool", "esptool.exe")
    if not os.path.exists(tool) and os.name == "nt":
        print("\nInstalling esptool (PlatformIO package)...")
        subprocess.run(pio() + ["pkg", "install", "--global", "--tool", "platformio/tool-esptool"], check=False)
    if os.path.exists(tool):
        os.makedirs(os.path.join(ADMIN, "tools"), exist_ok=True)
        shutil.copyfile(tool, os.path.join(ADMIN, "tools", "esptool.exe"))
        print(f"--> {os.path.join(ADMIN, 'tools', 'esptool.exe')}")
    else:
        print("Note: esptool.exe not found; put it in admin/tools so the Toolbox can flash.")


def main():
    wanted = set(sys.argv[1:])
    for path in sorted(glob.glob(os.path.join(ADMIN, "modules", "*", "module.json"))):
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
        if "project" not in meta or (wanted and meta["id"] not in wanted):
            continue
        build(meta, os.path.dirname(path))
    copy_esptool()          # after the builds: PlatformIO and its packages are installed by then
    print("\nDone.")


if __name__ == "__main__":
    main()
