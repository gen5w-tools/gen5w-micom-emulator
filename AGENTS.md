# MICOM emulator development

This project runs offline. Never connect to or control a vehicle or physical
head unit. Firmware is external and read-only. Execute firmware only using the
restricted Docker wrappers in scripts/micom. Host-side unit tests execute
Python device models only.

Put derived artifacts under analysis-output/<topic>-<YYYY-MM-DD>/; use fresh
output directories. Do not commit firmware, memory dumps, decrypted binaries,
or Android disks. Record hashes, commands, versions and observed limitations.
Keep application firmware untouched: simulate hardware registers, interrupts
and driver boundaries; never force application state or replace its handlers.

For Android integration, use emuctl from the gen5w-emulator project: start a
private instance for the tandem run, stop only that instance afterwards, and
retain its state. Run python3 -m unittest discover -s tests -v and the Docker
firmware validator when execution or device behavior changes.
