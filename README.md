# MICOM emulator

Offline execution of MICOM firmware using 32-bit PowerPC VLE semantics. The
firmware, RTOS, application tasks, CAN handlers and HU protocol code run
unchanged. Hardware contracts and absent board devices are simulated.
Firmware is supplied separately; none is committed here. Board tables and
driver addresses are firmware-specific: the defaults pin the tested image
(see `--expected-sha256` / `--expected-flash-sha256`), pass a different hash
to run another image.

Read [the assessment audit](docs/GAP_ASSESSMENT.md) before relying on old
message-plane/CAN descriptions of this hardware.

## Build and run

```sh
cd <checkout>
scripts/micom/build.sh
# Package a decrypted image into a NEW output directory.
python3 scripts/micom/package.py /path/to/micom_sw_decrypted.bin \
  analysis-output/packaged
# Override the default expected plaintext hash for a different image:
# python3 scripts/micom/package.py --expected-sha256 <sha256> ...
# The minimal profile intentionally remains in standby.
scripts/micom/run.sh analysis-output/packaged/flash.bin \
  analysis-output/standby --steps 6000000
# Attach the board fakes and apply physical power/switch inputs.
scripts/micom/run.sh analysis-output/packaged/flash.bin \
  analysis-output/acc-on --board --events profiles/acc-on.jsonl \
  --steps 12000000
# Override the default expected flash hash for a different image:
# scripts/micom/run.sh ... --expected-flash-sha256 <sha256>
```

The packager records the plaintext hash in the manifest; the runner records the
packaged flash hash because board tables and driver addresses are specific to
each firmware image. Both default to the tested image and accept an explicit
hash for other images. Output directories must be empty. Build uses pinned Python
and pypcode 3.3.3; firmware runs in Docker without networking, privileges or
host devices, with only scripts/firmware read-only and one output directory
writable. Docker is the supported execution method: it provides the pinned
toolchain, the compiled VLE language, and the sandbox. Ordinary host unit
tests execute device models only and need no Docker or firmware.

`--board` attaches classic FlexCAN, port-4 SPI memory, the port-0 switch monitor,
I²C with a 0x33 register slave, and the absent port-1 SPI board peer. Each can be
selected separately (`--can`, `--spi-memory`, `--switch-monitor`, `--i2c`,
`--spi-peer`). These are explicit hardware fakes, not full vehicle models.
The SPI peers' physical identities remain unresolved. See the audit for limits.

## Execution model

Stock QEMU has no PowerPC VLE instruction translator, so the runner uses
[pypcode](https://github.com/angr/pypcode) 3.3.3 with Ghidra SLEIGH semantics.
`scripts/micom/micom_vle.slaspec` composes stock `ppc_common`/`ppc_vle`/`evx`/
`quicciii` sources with 32-bit registers; no instruction encodings or semantics
were invented. The Docker build compiles this composition with pypcode's bundled
`sleigh`; `pcode_cpu.py` compiles supported integer p-code to cached Python and
interprets the rest. Unknown instructions fail explicitly, never become NOPs.

The image is flat: runtime address equals decrypted-file offset. The boot header
is `005a0000` with a VLE entry pointer; the packager preserves every input byte
and pads the absent tail with erased-flash `FF` up to `0x180000`. It also emits
a sectionless big-endian ELF32 `EM_PPC` image for import/debug tooling only —
an ELF wrapper does not make VLE executable in stock QEMU. SRAM is mapped at
`0x40000000–0x40017FFF`; flash is read-only and guest writes outside SRAM and
peripheral ranges fail. No decryption is performed by these scripts; their input
is an already decrypted update package.

## Android cable

Install `emuctl` from the published gen5w-emulator project, then use
the launcher:

```sh
emuctl build
python3 scripts/micom/tandem.py /path/to/flash.bin \
  analysis-output/tandem-run
```

It initializes MICOM first, then creates a private V015 HU with `--micom` and
connects the raw UART socket. This preserves the car's MCU-before-HU ordering
without a race between virtual MCU time and Android wall time. Interrupting the
launcher checkpoints MICOM, stops only its private HU, and retains all state.
Use `--steps N` for a bounded live run. The first six million MICOM instructions
are a separate prepared checkpoint, so the live budget counts after that.

For manual lifecycle control, create an initialized MICOM checkpoint using the
board profile, then `emuctl start V015 --micom`, inspect the instance directory,
and resume MICOM with `--uart-socket /path/to/instance/micom.sock`. QEMU waits
for the first cable connection before booting. Connect before using `emuctl
wait`; a late connection to an already-booted HU can miss its one-time startup
request. Firmware flushes early UART bytes during initialization, as real code
requires; the launcher avoids sending that request into the flush window.

The socket is a raw bidirectional UART stream. Android's real micomd opens a
separate PCI 16550 through `/dev/ttyMICOM0`; init creates that alias after `/dev`
is rebuilt and starts the daemon. Kernel uinput is enabled. The MICOM firmware
decides when UART-1 opens. Queues are bounded and the transport never generates
protocol responses. Connection loss fails explicitly; reconnect by resuming a
checkpoint with a new cable connection. A socket reconnect does not roll back
Android's state or guarantee application transaction replay.

`--steps 0` runs until interrupted. SIGINT/SIGTERM writes a checkpoint; Docker
forwards SIGTERM to the runner. Positive budgets count additional instructions,
including on resume. Time is synthetic (`--tick-steps`, default 10000), not wall
time or calibrated MCU cycles. Android and MICOM therefore do not share a
clock. Allow enough virtual time for firmware delays.

## Stimuli and checks

Events are JSONL records with an absolute instruction `step` and `kind`:

```json
{"step":2000000,"kind":"gpio","pin":99,"value":1}
{"step":2500000,"kind":"can","unit":0,"id":291,"data":"01020304"}
{"step":3000000,"kind":"switches","value":33299520}
```

CAN IDs are raw physical CAN identifiers, not micomlog's HU command words.
`period_steps` with `until_step` repeats a bounded stimulus. A `uart` event
carries raw hex bytes in `data`; UART fixtures can also include an absolute
`step` to preserve observed startup timing. There is no input
record for application RAM writes. `--gpio PIN=VALUE` sets initial levels;
`--switch-inputs` accepts the raw monitor word. The example profile asserts
GPIO 1 power-good, GPIO 91 to allow board initialization, then monitor input
bits 10 and 6 after two million instructions. Those are firmware-derived bit
positions; physical harness signal names are not claimed.

```sh
python3 -m unittest discover -s tests -v
scripts/micom/validate.sh /path/to/flash.bin /path/to/hu-to-micom.jsonl \
  analysis-output/validation
scripts/micom/run.sh /path/to/flash.bin analysis-output/resumed \
  --resume analysis-output/acc-on --steps 6000000
```

The validator runs 132 checks using real seed, serial read, checksum and isel
instructions with independent expected values and both CPU execution engines,
plus GPIO edge latching, masking, vector entry and acknowledgement.
`--interpret` selects the slower p-code interpreter for a full run.

Checkpoints preserve CPU/SRAM/MMIO, all new device state, pending input events,
UART ring/cable queues, timers and firmware/decoder hashes. They do not snapshot
Android. `live.jsonl` streams the full trace (`events.jsonl` aliases it),
`uartN-tx.bin` captures actual firmware output, and `result.json` reports hashes,
errors, interrupts and cable counts. `--trace-function ADDRESS` and
`--watch-address ADDRESS` enable explicit debugging probes. Trace memory is
bounded, but capture files grow on disk.

The board acceptance test replays three requests captured from the actual HU,
checks real firmware replies and their checksums, then compares a continuous
nine-million-instruction boot against a six-million checkpoint plus resume:

```sh
python3 scripts/micom/check_board.py /path/to/flash.bin \
  analysis-output/board-check
```

The separate CAN wake/filter scenario injects 61 zero-data frames into the
mailboxes configured by this firmware (it does not claim real ECU semantics):

```sh
scripts/micom/run.sh /path/to/flash.bin analysis-output/can-check \
  --can --events profiles/can-wake.jsonl --steps 5000000
```
