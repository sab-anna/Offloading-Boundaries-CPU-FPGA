// revbench.cpp - host benchmark for the revision experiments on the KV260.
//
// One program for all targets, following the protocol of the paper (Sections 3.4, 3.7, 3.8):
//   cpu1 : single-threaded loop nest
//   cpu4 : same loop nest, outer loop parallelized with OpenMP (thread binding via OMP_* env)
//   acc  : HLS accelerator through /dev/mem (control registers) and u-dma-buf (data buffer);
//          each invocation = (2) sync_for_device, (3) program + read back 6 registers,
//          (4) ap_start, (5) poll ap_done, (6) sync_for_cpu; (1) poison and (7) verify excluded.
//
// Two run types:
//   --grid           : all (N,K) of the paper, 1 warm-up + --reps timed runs each, one CSV row per run
//   --seconds S      : energy run at one (N,K): repeat invocations for >= S seconds and print the loop
//                      start/end on CLOCK_MONOTONIC, so that power samples can be aligned exactly
//
// Build on the KV260:  g++ -O3 -std=c++17 -fopenmp -o revbench revbench.cpp
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#include <fcntl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

using clk = std::chrono::steady_clock;  // CLOCK_MONOTONIC on Linux
static double mono_s() { return std::chrono::duration<double>(clk::now().time_since_epoch()).count(); }
static int64_t ns(clk::time_point a, clk::time_point b) {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(b - a).count();
}
[[noreturn]] static void die(const std::string &m) { fprintf(stderr, "revbench: %s\n", m.c_str()); exit(2); }

// ---------------------------------------------------------------- processor kernels (same loop nest)
static void kernel_cpu1(const uint32_t *in, uint32_t *out, int n, int k) {
  for (int i = 0; i < n; ++i) {
    uint32_t x = in[i], acc = 0;
    for (int j = 0; j < k; ++j) acc += (x ^ (uint32_t)j) * 1664525u + 1013904223u;
    out[i] = acc;
  }
}
static void kernel_cpu4(const uint32_t *in, uint32_t *out, int n, int k) {
#pragma omp parallel for
  for (int i = 0; i < n; ++i) {
    uint32_t x = in[i], acc = 0;
    for (int j = 0; j < k; ++j) acc += (x ^ (uint32_t)j) * 1664525u + 1013904223u;
    out[i] = acc;
  }
}

// ---------------------------------------------------------------- accelerator access
struct Times { int64_t cfg = 0, s2d = 0, kern = 0, s2c = 0, e2e = 0; };

struct Acc {
  bool dry = false;
  volatile uint32_t *regs = nullptr;
  uint32_t *buf = nullptr;  // u-dma-buf user mapping
  uint64_t phys = 0;
  size_t size = 0;
  uint32_t off_in = 0x10, off_out = 0x1c, off_n = 0x28, off_k = 0x30;
  int fd_off = -1, fd_size = -1, fd_dir = -1, fd_dev = -1, fd_cpu = -1;
  bool poll_sleep = false;
  std::vector<uint32_t> dryregs, drybuf;

  static std::string readfile(const std::string &p) {
    FILE *f = fopen(p.c_str(), "r");
    if (!f) die("cannot read " + p + " (" + strerror(errno) + ")");
    char b[256] = {0};
    if (!fgets(b, sizeof b, f)) b[0] = 0;
    fclose(f);
    return b;
  }
  static int openw(const std::string &p) {
    int fd = open(p.c_str(), O_WRONLY);
    if (fd < 0) die("cannot open " + p + " for writing (" + strerror(errno) + ")");
    return fd;
  }
  static void wr(int fd, unsigned long long v) {
    char b[32];
    int l = snprintf(b, sizeof b, "%llu", v);
    if (pwrite(fd, b, l, 0) != l) die(std::string("sysfs write failed: ") + strerror(errno));
  }

  void open_hw(uint64_t ctrl_base, const std::string &name) {
    if (dry) {
      dryregs.assign(0x40, 0);
      dryregs[0] = 4;  // ap_idle
      regs = dryregs.data();
      size = 16u << 20;
      drybuf.assign(size / 4, 0);
      buf = drybuf.data();
      phys = 0x38B00000;
      return;
    }
    const std::string sys = "/sys/class/u-dma-buf/" + name + "/";
    phys = strtoull(readfile(sys + "phys_addr").c_str(), nullptr, 0);
    size = strtoull(readfile(sys + "size").c_str(), nullptr, 0);
    int fdb = open(("/dev/" + name).c_str(), O_RDWR | O_SYNC);
    if (fdb < 0) die("cannot open /dev/" + name + " (" + strerror(errno) + ")");
    void *pb = mmap(nullptr, size, PROT_READ | PROT_WRITE, MAP_SHARED, fdb, 0);
    if (pb == MAP_FAILED) die("mmap of u-dma-buf failed");
    buf = (uint32_t *)pb;
    int fdm = open("/dev/mem", O_RDWR | O_SYNC);
    if (fdm < 0) die("cannot open /dev/mem (run as root)");
    void *pr = mmap(nullptr, 0x10000, PROT_READ | PROT_WRITE, MAP_SHARED, fdm, (off_t)ctrl_base);
    if (pr == MAP_FAILED) die("mmap of control registers failed");
    regs = (volatile uint32_t *)pr;
    fd_off = openw(sys + "sync_offset");
    fd_size = openw(sys + "sync_size");
    fd_dir = openw(sys + "sync_direction");
    fd_dev = openw(sys + "sync_for_device");
    fd_cpu = openw(sys + "sync_for_cpu");
  }
  // one sync request: offset, size, direction, trigger (direction 1 = to device, 2 = from device)
  void sync(int fd_trigger, size_t off, size_t len, int dir) {
    if (dry) return;
    wr(fd_off, off);
    wr(fd_size, len);
    wr(fd_dir, dir);
    wr(fd_trigger, 1);
  }
  uint32_t rd(uint32_t off) { return regs[off / 4]; }
  void wreg(uint32_t off, uint32_t v) { regs[off / 4] = v; }

  // one accelerator invocation on in = buf[0..n), out = buf[n..2n)
  void invoke(int n, int k, Times &t) {
    const uint64_t pin = phys, pout = phys + 4ull * n;
    if (!dry && !(rd(0) & 4u)) {  // wait for ap_idle
      double t0 = mono_s();
      while (!(rd(0) & 4u)) if (mono_s() - t0 > 5) die("accelerator not idle");
    }
    auto a = clk::now();
    sync(fd_dev, 0, 8ull * n, 1);                                   // (2)
    auto b = clk::now();
    const uint32_t vals[6] = {(uint32_t)pin, (uint32_t)(pin >> 32), (uint32_t)pout,
                              (uint32_t)(pout >> 32), (uint32_t)n, (uint32_t)k};
    const uint32_t offs[6] = {off_in, off_in + 4, off_out, off_out + 4, off_n, off_k};
    for (int r = 0; r < 6; ++r) wreg(offs[r], vals[r]);           // (3)
    for (int r = 0; r < 6; ++r)
      if (rd(offs[r]) != vals[r]) die("register read-back mismatch at offset " + std::to_string(offs[r]) +
                                      " (check the offsets in x<kernel>_hw.h and that the PS-side width of M_AXI_HPM0_FPD, register AFI_FS,"
                                      " matches the loaded design)");
    auto c = clk::now();
    wreg(0, 1u);                                                    // (4) ap_start
    if (dry) {                                                      // emulate the hardware
      kernel_cpu1(buf, buf + n, n, k);
      dryregs[0] = 2u | 4u;
    }
    const double tstart = mono_s();
    struct timespec ts = {0, 20000};
    while (!(rd(0) & 2u)) {                                         // (5) poll ap_done (clear-on-read)
      if (poll_sleep) nanosleep(&ts, nullptr);
      if (mono_s() - tstart > 60) die("timeout waiting for ap_done");
    }
    if (dry) dryregs[0] = 4u;
    auto d = clk::now();
    sync(fd_cpu, 4ull * n, 4ull * n, 2);                            // (6)
    auto e = clk::now();
    t.s2d = ns(a, b); t.cfg = ns(b, c); t.kern = ns(c, d); t.s2c = ns(d, e); t.e2e = ns(a, e);
  }
};

// ---------------------------------------------------------------- helpers
static std::vector<int> parse_list(const char *s) {
  std::vector<int> v;
  std::string t(s);
  size_t p = 0;
  while (p < t.size()) {
    size_t q = t.find(',', p);
    v.push_back((int)strtol(t.substr(p, q - p).c_str(), nullptr, 0));
    if (q == std::string::npos) break;
    p = q + 1;
  }
  return v;
}
static long count_mismatch(const uint32_t *a, const std::vector<uint32_t> &ref, int n) {
  long m = 0;
  for (int i = 0; i < n; ++i) m += (a[i] != ref[i]);
  return m;
}

int main(int argc, char **argv) {
  std::string mode, tag = "run", csv, udmabuf = "udmabuf0", poll = "spin";
  int n = 1 << 20, k = 1, reps = 20, campaign = 1;
  double seconds = 0;
  bool grid = false, dry = false;
  uint64_t ctrl = 0xA0000000ull;
  std::vector<int> NS = {1024, 4096, 16384, 65536, 262144, 1048576}, KS = {1, 4, 16, 64, 256};
  Acc acc;
  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    auto val = [&]() -> const char * { if (i + 1 >= argc) die("missing value for " + a); return argv[++i]; };
    if (a == "--mode") mode = val();
    else if (a == "--n") n = (int)strtol(val(), nullptr, 0);
    else if (a == "--k") k = (int)strtol(val(), nullptr, 0);
    else if (a == "--reps") reps = atoi(val());
    else if (a == "--seconds") seconds = atof(val());
    else if (a == "--grid") grid = true;
    else if (a == "--ns") NS = parse_list(val());
    else if (a == "--ks") KS = parse_list(val());
    else if (a == "--tag") tag = val();
    else if (a == "--campaign") campaign = atoi(val());
    else if (a == "--csv") csv = val();
    else if (a == "--ctrl") ctrl = strtoull(val(), nullptr, 0);
    else if (a == "--udmabuf") udmabuf = val();
    else if (a == "--poll") poll = val();
    else if (a == "--off-in") acc.off_in = (uint32_t)strtoul(val(), nullptr, 0);
    else if (a == "--off-out") acc.off_out = (uint32_t)strtoul(val(), nullptr, 0);
    else if (a == "--off-n") acc.off_n = (uint32_t)strtoul(val(), nullptr, 0);
    else if (a == "--off-k") acc.off_k = (uint32_t)strtoul(val(), nullptr, 0);
    else if (a == "--dry-run") dry = true;
    else die("unknown option " + a);
  }
  if (mode != "cpu1" && mode != "cpu4" && mode != "acc") die("--mode must be cpu1, cpu4 or acc");
  if (!grid && seconds <= 0) die("give --grid or --seconds S");
  if (!grid) { NS = {n}; KS = {k}; }
  const int nmax = *std::max_element(NS.begin(), NS.end());

  uint32_t *in, *out;
  std::vector<uint32_t> hin, hout;
  if (mode == "acc") {
    acc.dry = dry;
    acc.poll_sleep = (poll == "sleep");
    acc.open_hw(ctrl, udmabuf);
    if (8ull * nmax > acc.size) die("u-dma-buf too small for N=" + std::to_string(nmax));
  } else {
    hin.resize(nmax);
    hout.resize(nmax);
  }

  FILE *fcsv = nullptr;
  if (grid) {
    if (csv.empty()) die("--grid needs --csv FILE");
    bool exists = access(csv.c_str(), F_OK) == 0;
    fcsv = fopen(csv.c_str(), "a");
    if (!fcsv) die("cannot open " + csv);
    if (!exists) fprintf(fcsv, "tag,mode,campaign,n,k,rep,t_kern_ns,t_cfg_ns,t_s2d_ns,t_s2c_ns,t_e2e_ns,mismatches\n");
  }
  long total_mismatch = 0;

  for (int N : NS) {
    if (mode == "acc") { in = acc.buf; out = acc.buf + N; } else { in = hin.data(); out = hout.data(); }
    for (int i = 0; i < N; ++i) in[i] = (uint32_t)i;                 // x_i = i
    for (int K : KS) {
      std::vector<uint32_t> ref(N);
      kernel_cpu1(in, ref.data(), N, K);                              // software reference
      auto run_once = [&](Times &t) {
        if (mode == "acc") { acc.invoke(N, K, t); return; }
        auto a = clk::now();
        if (mode == "cpu1") kernel_cpu1(in, out, N, K); else kernel_cpu4(in, out, N, K);
        auto b = clk::now();
        t.kern = t.e2e = ns(a, b);
      };
      if (grid) {
        for (int r = 0; r <= reps; ++r) {                             // r = 0 is the warm-up
          for (int i = 0; i < N; ++i) out[i] = 0xDEADBEEFu;          // (1) poison
          Times t;
          run_once(t);
          long mm = count_mismatch(out, ref, N);                      // (7) verify
          total_mismatch += mm;
          if (r == 0) continue;
          fprintf(fcsv, "%s,%s,%d,%d,%d,%d,%lld,%lld,%lld,%lld,%lld,%ld\n", tag.c_str(), mode.c_str(), campaign,
                  N, K, r, (long long)t.kern, (long long)t.cfg, (long long)t.s2d, (long long)t.s2c,
                  (long long)t.e2e, mm);
        }
        fflush(fcsv);
        fprintf(stderr, "[%s c%d] N=%d K=%d done\n", tag.c_str(), campaign, N, K);
      } else {
        for (int i = 0; i < N; ++i) out[i] = 0xDEADBEEFu;
        Times t;
        run_once(t);                                                  // warm-up
        long reps_done = 0;
        double sk = 0, se = 0, sk2 = 0, se2 = 0;
        const double t0 = mono_s();
        double t1;
        do {
          run_once(t);
          sk += t.kern; se += t.e2e; sk2 += (double)t.kern * t.kern; se2 += (double)t.e2e * t.e2e; ++reps_done;
          t1 = mono_s();
        } while (t1 - t0 < seconds);
        long mm = count_mismatch(out, ref, N);
        total_mismatch += mm;
        const double mk = sk / reps_done, me = se / reps_done;
        const double sdk = reps_done > 1 ? std::sqrt(std::max(0.0, (sk2 - reps_done * mk * mk) / (reps_done - 1))) : 0.0;
        const double sde = reps_done > 1 ? std::sqrt(std::max(0.0, (se2 - reps_done * me * me) / (reps_done - 1))) : 0.0;
        printf("ENERGY tag=%s mode=%s n=%d k=%d reps=%ld loop_start=%.6f loop_end=%.6f "
               "mean_kern_ns=%.1f sd_kern_ns=%.1f mean_e2e_ns=%.1f sd_e2e_ns=%.1f mismatches=%ld poll=%s\n",
               tag.c_str(), mode.c_str(), N, K, reps_done, t0, t1, mk, sdk, me, sde, mm,
               mode == "acc" ? poll.c_str() : "-");
        fflush(stdout);
      }
    }
  }
  if (fcsv) fclose(fcsv);
  if (total_mismatch) { fprintf(stderr, "revbench: %ld mismatching elements\n", total_mismatch); return 1; }
  return 0;
}
