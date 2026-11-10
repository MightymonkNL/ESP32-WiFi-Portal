"""Makes a portable ESP32 Toolbox package for another PC: dist/ESP32-Toolbox-<date>.zip

Contents (no personal data: no admin/data, admin/backups or admin/external):
  ESP32 Toolbox/
    ESP32 Toolbox.exe
    modules/<id>/          module.json, *.py, README, firmware/*.bin
    tools/esptool.exe      + its license
    LEES MIJ.txt

Run build_exe.bat first (it builds the firmware and the exe).
"""

import glob
import os
import shutil
import sys
import time
import zipfile

ADMIN = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(ADMIN)
EXE = os.path.join(ROOT, "ESP32 Toolbox.exe")
PIO_ESPTOOL = os.path.join(os.path.expanduser("~"), ".platformio", "packages", "tool-esptool")

README = """ESP32 Toolbox
=============

1. Pak deze map uit, bijvoorbeeld naar Documenten\\ESP32 Toolbox.
2. Start "ESP32 Toolbox.exe". Windows kan waarschuwen omdat de exe niet ondertekend is:
   Meer informatie -> Toch uitvoeren.
3. Sluit het ESP32-bordje aan met een USB-C datakabel en klik op Search.

Bij het opstarten haalt het WiFi Portal-project de portals en instellingen van het bordje op,
dus op een nieuwe pc heb je alleen het bordje nodig.

Mappen die de Toolbox zelf aanmaakt (blijven op deze pc):
  data\\       je portal-inhoud en instellingen van de Toolbox
  backups\\    automatische backups per bordje (bevatten WiFi-wachtwoorden en surveyantwoorden)
  external\\   projecten die je met "+ Add project" toevoegt

Bijwerken naar een nieuwe versie: vervang de exe, modules\\ en tools\\; laat data\\, backups\\ en
external\\ staan.

tools\\esptool.exe is esptool van Espressif (GPL-2.0, zie tools\\esptool-LICENSE.txt),
https://github.com/espressif/esptool
"""


def main():
    if not os.path.exists(EXE):
        sys.exit("ESP32 Toolbox.exe not found: run build_exe.bat first.")
    esptool = os.path.join(ADMIN, "tools", "esptool.exe")
    if not os.path.exists(esptool):
        sys.exit("admin/tools/esptool.exe not found: run build_exe.bat (or admin/build_modules.py) first.")
    for mj in glob.glob(os.path.join(ADMIN, "modules", "*", "module.json")):
        if not os.path.isdir(os.path.join(os.path.dirname(mj), "firmware")):
            sys.exit(f"Firmware missing for {os.path.dirname(mj)}: run build_exe.bat first.")

    stage = os.path.join(ROOT, "dist", "ESP32 Toolbox")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    shutil.copy2(EXE, stage)
    shutil.copytree(os.path.join(ADMIN, "modules"), os.path.join(stage, "modules"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    os.makedirs(os.path.join(stage, "tools"))
    shutil.copy2(esptool, os.path.join(stage, "tools"))
    lic = os.path.join(PIO_ESPTOOL, "LICENSE")
    if os.path.exists(lic):
        shutil.copy2(lic, os.path.join(stage, "tools", "esptool-LICENSE.txt"))
    with open(os.path.join(stage, "LEES MIJ.txt"), "w", encoding="utf-8") as f:
        f.write(README)

    out = os.path.join(ROOT, "dist", f"ESP32-Toolbox-{time.strftime('%Y-%m-%d')}.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for folder, _, files in os.walk(stage):
            for name in files:
                full = os.path.join(folder, name)
                z.write(full, os.path.relpath(full, os.path.dirname(stage)))
    print(f"{out} ({os.path.getsize(out) // (1024 * 1024)} MB)")


if __name__ == "__main__":
    main()
