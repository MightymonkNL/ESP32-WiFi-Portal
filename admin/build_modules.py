"""Builds the firmware of every module and copies it into admin/modules/<id>/firmware/.

Also copies esptool.exe (from PlatformIO) into admin/tools, so the Toolbox can flash.
Usage: python admin/build_modules.py [module-id ...]
"""

import glob
import json
import os
import shutil
import subprocess
import sys

ADMIN = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(ADMIN)
PIO_HOME = os.path.join(os.path.expanduser("~"), ".platformio")


def pio():
    found = shutil.which("pio") or shutil.which("platformio")
    if found:
        return found
    exe = os.path.join(PIO_HOME, "penv", "Scripts", "pio.exe")
    if os.path.exists(exe):
        return exe
    sys.exit("PlatformIO (pio) not found. Install PlatformIO first.")


def build(meta, folder):
    project = os.path.normpath(os.path.join(ROOT, meta["project"]))
    print(f"\n=== {meta['name']} ({meta['id']}) - {project}")
    subprocess.run([pio(), "run", "-d", project], check=True)
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


def main():
    wanted = set(sys.argv[1:])
    tool = os.path.join(PIO_HOME, "packages", "tool-esptool", "esptool.exe")
    if os.path.exists(tool):
        os.makedirs(os.path.join(ADMIN, "tools"), exist_ok=True)
        shutil.copyfile(tool, os.path.join(ADMIN, "tools", "esptool.exe"))
    else:
        print("Note: esptool.exe not found in PlatformIO; the Toolbox looks for it in admin/tools.")
    for path in sorted(glob.glob(os.path.join(ADMIN, "modules", "*", "module.json"))):
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
        if "project" not in meta or (wanted and meta["id"] not in wanted):
            continue
        build(meta, os.path.dirname(path))
    print("\nDone.")


if __name__ == "__main__":
    main()
