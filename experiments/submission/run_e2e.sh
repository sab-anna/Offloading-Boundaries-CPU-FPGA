#!/bin/bash
# Sweep end-to-end FPGA measurements over the (N, K) grid.
# Usage: sudo ./run_e2e.sh LABEL [R] | tee results/e2e_LABEL.csv
# LABEL: P1 or P4 (the corresponding app must be loaded with xmutil)
# R: repetitions per configuration, rep 0 = warm-up (default 21)
set -e
LABEL=${1:?"usage: sudo ./run_e2e.sh LABEL [R]"}
R=${2:-21}
cd "$(dirname "$0")"
echo "design,N,K,bytes,rep,cfg_us,s2d_us,kern_us,s2c_us,total_us,errors"
for N in 1024 4096 16384 65536 262144 1048576; do
  for K in 1 4 16 64 256; do
    ./fpga_bench_e2e "$LABEL" "$N" "$K" "$R"
  done
done
