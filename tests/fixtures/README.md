# HU startup fixture

`hu-startup.jsonl` contains three captured HU-to-MICOM requests: the initial
0x0101 handshake followed by two settings queries (0x0C5F and 0x0C01).
All have FF framing, a big-endian length including checksum, and XOR zero.

`step` records the receiving MICOM instruction time from the capture run. The
two settings frames arrived together after fragmented stream headers; both use
the step at which the complete frames were available. The fixture preserves
input order and the initial UART flush window. These are emulation timestamps,
not measured car timing. Subsequent fixture pacing remains the runner's
synthetic spacing. No firmware, application RAM or fabricated response is
included.
