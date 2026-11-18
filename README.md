# CPU–FPGA offloading boundaries on the AMD Kria KV260

Code, hardware sources and measurement data for the article

> A. Sabatini, R. Bagnato, D.P. Pau,  L. Vollero, *Offloading Boundaries in a Heterogeneous Embedded SoC: Characterization on CPU-FPGA*. 

The study runs one parametric microbenchmark, bit-identical, on one and four Arm Cortex-A53 cores and on two HLS accelerators in the programmable logic (PL) of the Zynq UltraScale+ MPSoC, and measures where offloading pays off as a function of the problem size `N` and of the computational intensity `K`:

```
y[i] = sum_{j=0}^{K-1} ((x[i] XOR j) * 1664525 + 1013904223)   (mod 2^32),   i = 0 .. N-1
```

## Repository layout

```
├── hls/                    Vitis HLS sources of the two accelerators
│   ├── p1/                 single-lane design   (top: microbench)
│   ├── p4/                 four-lane design     (top: microbench_p4) + C testbench
│   └── ip/                 exported IP cores used in the block designs
├── vivado/                 block designs (design_1.bd) of both designs + build.tcl
├── experiments/
│   ├── revision/           revision campaign: host program, power logger, run script, analysis, figures
│   └── submission/         first campaign (submitted manuscript): host program and run scripts
├── data/
│   ├── revision_2026-11-09/    raw data, tables, figure data and figures of the revision campaign
│   └── submission_2026-10-06/  raw data of the first campaign
├── reports/                HLS synthesis reports and logs, Vivado utilization and timing reports
└── tools/                  bit2bin.py (.bit -> .bit.bin), set_hpm0_width.sh (AFI_FS register)
```

Bitstreams, `.bit.bin` files and XSAs are not in git: they are attached to the GitHub release (see [Binaries](#binaries)).

## Platform and tools

| Item | Value |
|---|---|
| Board | AMD Kria KV260 Vision AI Starter Kit, K26 SOM, XCK26-SFVC784-2LV-C |
| OS | Kria Ubuntu 22.04, kernel `5.15.0-1027-xilinx-zynqmp` |
| Processor | 4× Cortex-A53 at 1333 MHz, `userspace` cpufreq governor |
| PL clock | `pl_clk0` = 100 MHz (99.999 MHz) |
| FPGA tools | Vivado and Vitis HLS 2025.2 |
| Host compiler | GCC 11.4.0 |
| DMA buffer | [u-dma-buf](https://github.com/ikwzm/udmabuf) <!-- TODO: version (modinfo u-dma-buf) -->, `udmabuf0` = 16 MiB, opened with `O_SYNC` (non-cacheable mapping) |
| Power sensor | INA260 of the K26 SOM, read through hwmon (revision campaign only) |

## Accelerator designs

| | P=1 | P=4 |
|---|---|---|
| HLS source | `hls/p1/kernel.cpp` | `hls/p4/kernel_p4.cpp` |
| IP (VLNV) | `xilinx.com:hls:microbench:1.0` | `user.org:hls:microbench_p4:1.0` |
| xmutil app | `microbench` | `microbench_p4` |
| `m_axi` ports (`gmem0`, `gmem1`) | 32 bit | 32 bit |
| `S_AXI_HP0_FPD` (data path) | 128 bit | 128 bit |
| `M_AXI_HPM0_FPD` (control path) | **128 bit** | **32 bit** |
| AFI_FS setting after `loadapp` | `set_hpm0_width.sh 128` | `set_hpm0_width.sh 32` |
| Bitstream generated | 2026-09-07 17:07:27 | 2026-10-06 22:04:24 |

Control registers (AXI4-Lite, base `0xA0000000`): `0x00` ap_ctrl, `0x10`/`0x14` input address (low/high), `0x1C`/`0x20` output address (low/high), `0x28` N, `0x30` K.

## Binaries

Attached to the GitHub release, in the folder layout of `/lib/firmware/xilinx/`:

| File | SHA-256 |
|---|---|
| `microbench/microbench.bit.bin` | `0cbaeacb52d1ddcbccc376cb1fb59e77dd1a0e8d1f2c9f3bf98492e15ffc4e4e` |
| `microbench/kv260_microbench.bit` | `a890e5b09fe3d25d4c08a7d66aa76580c269e5faa84e9d3354ae2865f5ded511` |
| `microbench/kv260_microbench.xsa` | `57764d9f3e875457c1fd11fd7f4d3ddfe741fbc85203fb8d03dc78cef511c246` |
| `microbench_p4/kv260_microbench_p4.bit.bin` | `3cfd6959df8b4c16e24602b810afce7a754851a0c474c00a6e0366c328a9e301` |
| `microbench_p4/kv260_microbench_p4.bit` | `fac845e17728b687c21ca38741168c571ae3c6acca64707fa5a34486e656d9e0` |
| `microbench_p4/kv260_microbench_p4.xsa` | `bd0891f64b1f42cc13e777b42bbdc9ba6e368b688c3e0b6dc99309b5d65aae10` |

The `.bit.bin` files were obtained from the `.bit` files with `tools/bit2bin.py`. Always check the hash of the file installed on the board: an outdated bitstream with a different `M_AXI_HPM0_FPD` width was the cause of the silent register failures described in the article.

## Reproducing the results

### 1. HLS (optional: the exported cores are in `hls/ip/`)

From `hls/p1` or `hls/p4`, with Vitis 2025.2:

```bash
vitis-run --mode hls --csim    --config hls_config.cfg --work_dir build   # P=4 only (C testbench)
v++ -c --mode hls              --config hls_config.cfg --work_dir build   # synthesis
vitis-run --mode hls --cosim   --config hls_config.cfg --work_dir build   # P=4 only
vitis-run --mode hls --package --config hls_config.cfg --work_dir build   # IP for Vivado
```

The same steps are available in the Vitis Unified IDE. Reference reports: `reports/hls/`.

### 2. Vivado

```bash
vivado -mode batch -source vivado/build.tcl -tclargs p4      # or p1
```

The script creates the project in `build/vivado_<design>/` from `vivado/<design>/design_1/design_1.bd` and the core in `hls/ip/`, then writes the bitstream, the XSA and the reports. It needs the Kria K26 SOM board files (`xilinx.com:kv260_som:part0:1.4`). A rebuilt bitstream is functionally equivalent but not byte-identical to the measured one: to repeat the measurements exactly, use the release binaries. Reference reports: `reports/vivado/`.

### 3. Bitstream for the board

```bash
python3 tools/bit2bin.py build/vivado_p4/kv260_microbench_p4.bit kv260_microbench_p4.bit.bin
```

Then build the overlay and install the app.

### 4. Board setup (after every boot)

```bash
sudo insmod u-dma-buf.ko udmabuf0=16777216        # 16 MiB physically contiguous buffer
sudo xmutil unloadapp
sudo xmutil loadapp microbench_p4                  # or microbench
sudo tools/set_hpm0_width.sh 32                    # 128 for microbench
```

### 5. Measurements: revision campaign (`experiments/revision/`)

| File | Role |
|---|---|
| `revbench.cpp` | single host program for CPU-1, CPU-4 (OpenMP) and the accelerators; per invocation it measures T_cfg, T_s2d, T_kern, T_s2c and verifies every output element |
| `power_logger.py` | samples SOM power (INA260) and on-chip temperatures, aligned with the measurement loop (CLOCK_MONOTONIC) |
| `quiet_system.sh` | stops the desktop and background services, fixes the APU frequency, saves a snapshot of the system state |
| `run_all.sh` | runs the whole campaign, then `analyze.py` and `make_figures.py` |
| `analyze.py` | tables and figure data from a results folder (standard library only) |
| `make_figures.py` | figures (PDF and PNG) from the figure data (needs matplotlib) |

Edit the CONFIG block at the top of `run_all.sh` (apps, register offsets, u-dma-buf name), then run it **over SSH inside tmux**: `quiet_system.sh` stops the graphical session and several services until the next reboot.

```bash
cd experiments/revision
sudo tmux new -s rev
./run_all.sh                    # about 40 minutes; PHASES=energy ./run_all.sh for energy only
```

`run_all.sh` sets AFI_FS after every `loadapp` from the fourth field of each `APPS` entry (`P1:microbench:100:128 P4:microbench_p4:100:32`). The results go to `results_<date>/`. The campaign of the article is in `data/revision_2026-11-09/`.

What is measured:
- timing grid: N = 2^10 … 2^20, K = 1, 4, 16, 64, 256; one warm-up and 20 timed runs per configuration; CPU-1 once, CPU-4 with threads bound to cores (`OMP_PROC_BIND=close OMP_PLACES=cores`) five times, CPU-4 with unbound threads once, P=1 and P=4 once;
- energy at N = 2^20 for every K: invocations repeated for at least 10 s, three independent repetitions per configuration, accelerators with busy polling and with `usleep` polling; idle power is measured before the processor runs, after loading each design and at the end.

### 6. Tables and figures

```bash
cd experiments/revision
python3 analyze.py ../../data/revision_2026-11-09 > ../../data/revision_2026-11-09/tables/summary.txt
pip install -r requirements.txt
python3 make_figures.py ../../data/revision_2026-11-09
```

Statistics: timing is reported as the median of the 20 timed runs ± standard deviation, energy as the mean of the 3 repetitions ± standard deviation; the standard deviation of ratios is propagated. `grid_stats.csv` also gives mean, IQR, CV and bootstrap 95% confidence intervals.

| Output (`data/revision_2026-11-09/`) | Content |
|---|---|
| `tables/tab_cpu.tex` | processor baselines (`tab:cpu`) |
| `tables/tab_kernel_P1.tex`, `tab_kernel_P4.tex` | kernel times of the accelerators (`tab:p1`, `tab:p4`) |
| `tables/tab_vscpu_P1.tex`, `tab_vscpu_P4.tex` | accelerators vs processor at N = 2^20 (`tab:p1cpu`, `tab:p4cpu`) |
| `tables/tab_predict.tex` | per-element cost, measured and predicted (`tab:predict`) |
| `tables/tab_e2e.tex` | end-to-end decomposition (`tab:e2e`) |
| `tables/tab_energy.tex`, `tab_energy_sleep.tex` | energy per element (`tab:energy`, `tab:energy_sleep`) |
| `tables/tab_dispersion.tex` | run-to-run dispersion (`tab:dispersion`) |
| `tables/text_values.txt` | numbers quoted in the text, by section |
| `tables/summary.txt` | summary of the campaign and of the model fits |
| `tables/*.csv` | all statistics (`grid_stats.csv`, `best_cpu4.csv`, `energy.csv`, `temperatures.csv`) |
| `figures/fig_kernel_P1`, `fig_kernel_P4` | kernel time vs N with the calibrated model |
| `figures/fig_profit` | per-element cost vs K: measured, calibrated and predicted |
| `figures/fig_offloading_map` | offloading map over (N, K) |
| `figures/fig_energy` | energy per element vs K |

`make_figures.py` only reads `figdata/`, so figures can be restyled without repeating the analysis.

### First campaign (`experiments/submission/`)

The data of the submitted manuscript were measured on 2026-10-06 with:

```bash
g++ -O2 -std=c++17 fpga_bench_e2e.cpp -o fpga_bench_e2e
sudo ./run_e2e.sh P4 | tee e2e_p4.csv              # P=4 loaded, AFI_FS 32 bit
sudo ./run_e2e.sh P1 | tee e2e_p1.csv              # P=1 loaded, AFI_FS 128 bit
./run_cpu_grid.sh | tee cpu_grid.csv               # CPU-1 and CPU-4 (unbound threads)
for r in 1 2 3; do for N in 1024 4096 16384 65536 262144 1048576; do for K in 1 4 16 64 256; do
  printf "run$r,"; OMP_NUM_THREADS=4 OMP_PROC_BIND=close OMP_PLACES=cores ./cpu_bench_omp $N $K
done; done; done | tee cpu4_pinned.csv
```

`cpu_bench` and `cpu_bench_omp` were compiled with `g++ -O3 -std=c++17` (`-fopenmp` for the second). <!-- TODO: add cpu_bench.cpp and cpu_bench_omp.cpp -->

## Data formats

### `data/revision_2026-11-09/`

| Path | Content |
|---|---|
| `grid/grid.csv` | one row per timed run: `tag, mode, campaign, n, k, rep, t_kern_ns, t_cfg_ns, t_s2d_ns, t_s2c_ns, t_e2e_ns, mismatches` (times in ns; warm-up runs not included; `rep` = 1…20) |
| `energy/index.csv` | list of energy runs and of the matching power and idle files |
| `energy/E_*.out` | output of `revbench` and `power_logger.py` for each energy run (loop start/end, mean times, mean power, energy) |
| `energy/idle_*.txt`, `energy/fpl.csv` | idle power summaries; PL clock per design |
| `power/*.csv` | power (W) and temperatures (°C) sampled every 20 ms: `t_s, mono_s, power_W, ams:*` |
| `state/system_state.txt` | system state during the campaign (kernel, governor, services, load) |
| `logs/` | logs of `run_all.sh` and of each grid campaign |
| `tables/`, `figdata/`, `figures/` | outputs of `analyze.py` and `make_figures.py` |

Tags: `CPU1` single core; `CPU4b` four threads bound to cores (campaigns 1–5); `CPU4u` four unbound threads; `P1`, `P4` accelerators; `_sleep` = accelerator polled with `usleep`. All 5400 timed runs produced zero mismatching elements.

### `data/submission_2026-10-06/`

| File | Content |
|---|---|
| `e2e_p1.csv`, `e2e_p4.csv` | one row per run: `design, N, K, bytes, rep, cfg_us, s2d_us, kern_us, s2c_us, total_us, errors` (µs); `rep` 0 is the warm-up and is excluded from the analysis |
| `cpu_grid.csv` | `impl, N, K, bytes, median_us, checksum`; each row is the median of 20 runs after one warm-up; `cpu4` = unbound threads |
| `cpu4_pinned.csv` | no header: `run, N, K, bytes, median_us, checksum`; CPU-4 with threads bound to cores, three runs |

The processor programs of this campaign recorded only the median of each configuration, so its CPU values have no standard deviation. The revision campaign records every run.

## Known pitfalls

- **AFI_FS must match the bitstream.** If the PS side of `M_AXI_HPM0_FPD` and the block design disagree on the width, writes to control registers at offsets that are not multiples of the bus width are silently discarded and read back as zero. `xmutil loadapp` does not set the register: use `tools/set_hpm0_width.sh` (or `run_all.sh`, which does it automatically). The host programs read back every argument and stop on a mismatch.
- **Check the bitstream hash** on the board against `SHA256SUMS` before measuring.
- `quiet_system.sh` changes the system state until the next reboot.
- The IP address of the SSH client was removed from `state/system_state.txt`.




