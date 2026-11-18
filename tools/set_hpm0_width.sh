#!/usr/bin/env bash
# set_hpm0_width.sh <32|64|128>
# Sets the PS side of M_AXI_HPM0_FPD (register AFI_FS in FPD_SLCR, address 0xFD615000, bits [9:8]).
# It must match the width of M_AXI_HPM0_FPD in the block design of the loaded bitstream:
#   P=1 design (microbench)    -> 128
#   P=4 design (microbench_p4) -> 32
# xmutil loadapp does not change this register. A mismatch silently discards control-register
# writes at offsets that are not multiples of the bus width (see README, "Known pitfalls").
# Run as root, after every xmutil loadapp.
set -euo pipefail
case "${1:-}" in
  32)  v=0 ;;
  64)  v=1 ;;
  128) v=2 ;;
  *) echo "usage: sudo $0 <32|64|128>" >&2; exit 1 ;;
esac
ADDR=0xFD615000
old=$(busybox devmem $ADDR 32)
new=$(printf '0x%08X' $(( (old & ~0x300) | (v << 8) )))
busybox devmem $ADDR 32 "$new"
echo "AFI_FS: $old -> $(busybox devmem $ADDR 32)  (M_AXI_HPM0_FPD $1-bit)"
