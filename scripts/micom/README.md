# MICOM runner tools

This directory is part of the standalone [MICOM emulator](../../README.md).
Use that guide for current build, board stimulus, Android cable and checkpoint
commands. [The assessment audit](../../docs/GAP_ASSESSMENT.md) corrects the old
SPI/CAN/message-plane interpretations.

- build.sh, run.sh: pinned build and restricted Docker execution.
- package.py: decrypted image → flat flash, ELF and provenance manifest.
  Accepts --expected-sha256 to pin a different plaintext image.
- micom.py, pcode_cpu.py, micom_vle.slaspec: 32-bit VLE execution.
- devices.py, inputs.py: hardware models and physical input events.
- validate.sh, validate.py: real-firmware checks with both CPU engines.
- extract_frames.py: direction-preserving captured UART fixtures.
- inspect.py: offline VLE disassembly helper.
