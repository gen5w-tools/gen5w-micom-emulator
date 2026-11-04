# Gap assessment audit — 2026-10-18

This audit concerns the packaged V015 flash SHA-256
`113b94238cceeb3134e85bc6045fbf892c2711de3d413c33939690116d4bebf4`.
Addresses and driver hooks are specific to that image. The original assessment
mixed up several hardware blocks. No firmware bytes or application state are
patched by the new board profile.

| Supplied claim | Finding and implementation |
| --- | --- |
| Kernel shared-RAM message ingress at `0x4000D318`, publisher `0xA5140`, doorbells `0x1A2/0x1A4/0x3A4` | Incorrect interpretation. `0xA501C` emits SPI command 3 and a 16-bit address through port 4, CS 0. Companion writes use command 2 and write-enable 6. The supposed doorbells are memory offsets; `0x1A2`/`0x3A2` hold mirrored signatures. A RAM-record injector here would corrupt persistence, not reproduce vehicle ingress. Implemented an external SPI memory model with chip-select continuity, status, write enable, address wrap and snapshots instead. |
| Wake state can be injected using `0x73C32` source bytes | These are application-derived state. `0x74020` receives a switch-monitor callback and sets the debounced wake input; `0x73EE8` supplies another wake source. Implemented the SPI-0/CS-0 switch monitor at the driver boundary, including physical input polarity and parity. No direct writes to the source bytes. |
| `0x0802`, `0x0149`, `0x0109` micomlog records are CAN keepalive/ACC frames | They are commands in the HU serial protocol. The CAN mailboxes independently filter 11-bit identifiers. Recorded serial frames remain usable UART fixtures, but are not reinterpreted as CAN frames. |
| Six CAN units at `0xFFF90000–0xFFFA4000` | These addresses belong to DSPI. Firmware FlexCAN accesses are at `0xFFFC0000`, `0xFFFC4000`, `0xFFFC8000`. Implemented six FlexCAN register banks at 0x4000 spacing, mailbox filtering, masks, freeze/reset acknowledgement, RX lock/rearm, TX completion, W1C flags and firmware vector routing. This is classic CAN mailbox simulation, without electrical faults, bit timing or multi-node arbitration. |
| GPIO edges missing | Valid. Implemented SIUL EIRQ and WKPU edge latches, enable masks, W1C acknowledgement and their firmware IRQ groups. Inputs are instruction-timed events or initial pin levels. |
| UART-1 opens only when firmware decides | Valid. The cable queues raw bytes, and RX completion feeds only an actually opened firmware ring. Firmware serial open/close remains real code. |
| micomd disabled and not started in the original HU profile | Valid for the tested V015 template. Added explicit `emuctl start ... --micom`; Android init starts the actual daemon after creating its device alias. |
| ttyTCC2 missing under virt | Valid. Added a separate PCI 16550 at slot 5 and a raw `micom.sock` chardev. Android init creates `/dev/ttyMICOM0 -> /dev/ttyS1`, after init rebuilds `/dev`. The boot wrapper verifies the expected PCI/tty mapping. GPS stays separate. Immutable templates are not changed. |
| An explicit micomd trigger is required | Valid. The opt-in ramdisk adds `start micomd` at post-fs-data. Also enabled kernel uinput, required by the real daemon. Live V015 shows the daemon holding the UART and uinput descriptors. QEMU waits for the initial cable connection so Android's startup request is preserved. |

Additional missing hardware found during cold boot: the firmware enables PIT
channel 2 as well as channel 0. Both now have independent deadlines and level
interrupts. The board status task also performs I²C transactions at
`0xFFE30000` and waits for IRQ 125. The I²C controller model supplies byte
completion, repeated START, receive pipelining and reset/STOP behavior; an
explicit 0x33 register slave stands in for the absent board device. The next
blocking transfer was SPI port 1: firmware sends a length-prefixed selector
request and clocks a status/data reply. A separate zero-initialized register
peer models that absent device's request/reply boundary. It does not claim to
reproduce its internal processor, diagnostics, radio or vehicle behavior.

The switch-monitor and I²C slave part numbers are unresolved. Their wire/driver
contracts are supported by firmware observations; they are not claimed to model
the physical components' diagnostics or analog behavior. ADC remains an
uncalibrated configurable 10-bit input. Time is instruction-derived, not cycle
accurate; polled PIT delay completion is synchronous.

The corrected peripheral map agrees with NXP's published map:
[MPC5606S reference manual](https://www.nxp.com/docs/en/reference-manual/PXD10RM.pdf).
Firmware transactions, rather than a presumed exact MCU part number, determine
the implemented contracts.

The supplied list omitted startup ordering. A late UART connection receives
periodic `0x0149`/`0x0802` requests but misses Android's initial `0x0101`.
MICOM intentionally restricts those periodic commands before that handshake.
With a connection from boot, unchanged firmware replies `0x8101`, sets its
handshake flag (status bit 15) and handles normal settings requests. The
launcher prepares the MCU before starting its HU, matching their physical
power ordering and avoiding dependence on relative emulator speed.
