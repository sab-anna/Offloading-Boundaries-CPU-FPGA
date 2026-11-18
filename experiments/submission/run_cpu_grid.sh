#!/bin/bash
# Processor baselines (CPU-1, CPU-4) on the same (N, K) grid as the FPGA sweep.
# Uses the existing cpu_bench / cpu_bench_omp binaries (1 warm-up + 20 runs, median).
# Usage: ./run_cpu_grid.sh | tee results/cpu_grid.csv
set -e
cd "$(dirname "$0")"
echo "impl,N,K,bytes,median_us,checksum"
for N in 1024 4096 16384 65536 262144 1048576; do
  for K in 1 4 16 64 256; do
    printf "cpu1,"; ./cpu_bench "$N" "$K"
    printf "cpu4,"; OMP_NUM_THREADS=4 ./cpu_bench_omp "$N" "$K"
  done
done
