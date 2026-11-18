// fpga_bench_e2e.cpp
// End-to-end timing of the HLS microbenchmark accelerator on the Kria KV260.
//
// Measures, for every repetition, the four terms of
//   T_e2e = T_cfg + T_s2d + T_kern + T_s2c
// and verifies every output element against the software reference.
//
// Usage:  sudo ./fpga_bench_e2e LABEL N K R
// Output: R CSV lines (no header):
//   label,N,K,bytes,rep,cfg_us,s2d_us,kern_us,s2c_us,total_us,errors
// rep 0 is a warm-up run and is meant to be discarded in the analysis.
//
// Build:  g++ -O2 -std=c++17 src/fpga_bench_e2e.cpp -o fpga_bench_e2e

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fcntl.h>
#include <string>
#include <sys/mman.h>
#include <unistd.h>

static constexpr uint64_t CTRL_BASE = 0xA0000000ULL;
static constexpr size_t   CTRL_SIZE = 0x10000;

// HLS AXI4-Lite register map (same for microbench and microbench_p4)
static constexpr uint32_t REG_AP_CTRL = 0x00;
static constexpr uint32_t REG_IN_LO   = 0x10;
static constexpr uint32_t REG_IN_HI   = 0x14;
static constexpr uint32_t REG_OUT_LO  = 0x1c;
static constexpr uint32_t REG_OUT_HI  = 0x20;
static constexpr uint32_t REG_N       = 0x28;
static constexpr uint32_t REG_K       = 0x30;

static const char*       UDMABUF_DEV = "/dev/udmabuf0";
static const std::string UDMABUF_SYS = "/sys/class/u-dma-buf/udmabuf0";

static constexpr double KERNEL_TIMEOUT_US = 60e6;  // 60 s

using clk = std::chrono::steady_clock;

static double us_since(clk::time_point t0)
{
    return std::chrono::duration<double, std::micro>(clk::now() - t0).count();
}

static uint64_t read_sysfs_u64(const std::string& path, int base)
{
    FILE* f = std::fopen(path.c_str(), "r");
    if (!f) { std::perror(path.c_str()); std::exit(1); }
    char buf[64] = {0};
    if (!std::fgets(buf, sizeof buf, f)) {
        std::fprintf(stderr, "Cannot read %s\n", path.c_str());
        std::exit(1);
    }
    std::fclose(f);
    return std::strtoull(buf, nullptr, base);
}

static void write_sysfs(const std::string& path, const std::string& value)
{
    int fd = open(path.c_str(), O_WRONLY);
    if (fd < 0) { std::perror(path.c_str()); std::exit(1); }
    if (write(fd, value.data(), value.size()) != static_cast<ssize_t>(value.size())) {
        std::perror(path.c_str());
        std::exit(1);
    }
    close(fd);
}

// Cache maintenance through u-dma-buf: dir = "sync_for_device" or "sync_for_cpu"
static void sync_region(size_t offset, size_t size, const char* dir)
{
    write_sysfs(UDMABUF_SYS + "/sync_offset", std::to_string(offset));
    write_sysfs(UDMABUF_SYS + "/sync_size", std::to_string(size));
    write_sysfs(UDMABUF_SYS + "/" + dir, "1");
}

static uint32_t reference(uint32_t x, long k)
{
    uint32_t acc = 0;
    for (long j = 0; j < k; ++j)
        acc += (x ^ static_cast<uint32_t>(j)) * 1664525u + 1013904223u;
    return acc;
}

int main(int argc, char** argv)
{
    if (argc != 5) {
        std::fprintf(stderr, "Usage: sudo %s LABEL N K R\n", argv[0]);
        return 1;
    }

    const char* label = argv[1];
    const long  n     = std::atol(argv[2]);
    const long  k     = std::atol(argv[3]);
    const int   reps  = std::atoi(argv[4]);

    if (n <= 0 || k <= 0 || reps <= 0) {
        std::fprintf(stderr, "N, K and R must be > 0\n");
        return 1;
    }

    const size_t   bytes       = static_cast<size_t>(n) * sizeof(uint32_t);
    const uint64_t buffer_size = read_sysfs_u64(UDMABUF_SYS + "/size", 10);
    const uint64_t buffer_phys = read_sysfs_u64(UDMABUF_SYS + "/phys_addr", 0);

    if (2 * bytes > buffer_size) {
        std::fprintf(stderr, "u-dma-buf too small: need %zu bytes, have %llu\n",
                     2 * bytes, static_cast<unsigned long long>(buffer_size));
        return 1;
    }

    // Input at offset 0, output immediately after the input.
    const uint64_t input_phys  = buffer_phys;
    const uint64_t output_phys = buffer_phys + bytes;

    int buf_fd = open(UDMABUF_DEV, O_RDWR | O_SYNC);
    if (buf_fd < 0) { std::perror("open /dev/udmabuf0"); return 1; }

    void* buffer = mmap(nullptr, buffer_size, PROT_READ | PROT_WRITE, MAP_SHARED, buf_fd, 0);
    if (buffer == MAP_FAILED) { std::perror("mmap udmabuf0"); return 1; }

    auto* input  = static_cast<uint32_t*>(buffer);
    auto* output = reinterpret_cast<uint32_t*>(static_cast<uint8_t*>(buffer) + bytes);

    int mem_fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (mem_fd < 0) { std::perror("open /dev/mem"); return 1; }

    void* ctrl_map = mmap(nullptr, CTRL_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED, mem_fd, CTRL_BASE);
    if (ctrl_map == MAP_FAILED) { std::perror("mmap AXI-Lite"); return 1; }

    volatile uint32_t* regs = static_cast<volatile uint32_t*>(ctrl_map);
    auto reg_write = [&](uint32_t off, uint32_t v) { regs[off / 4] = v; __sync_synchronize(); };
    auto reg_read  = [&](uint32_t off) -> uint32_t { return regs[off / 4]; };

    const uint32_t offs[6] = {REG_IN_LO, REG_IN_HI, REG_OUT_LO, REG_OUT_HI, REG_N, REG_K};
    const uint32_t vals[6] = {
        static_cast<uint32_t>(input_phys & 0xffffffffULL),
        static_cast<uint32_t>(input_phys >> 32),
        static_cast<uint32_t>(output_phys & 0xffffffffULL),
        static_cast<uint32_t>(output_phys >> 32),
        static_cast<uint32_t>(n),
        static_cast<uint32_t>(k)};

    for (int r = 0; r < reps; ++r) {
        // (1) Initialize input, poison output (not timed)
        for (long i = 0; i < n; ++i) {
            input[i]  = static_cast<uint32_t>(i);
            output[i] = 0xDEADBEEFu;
        }
        __sync_synchronize();

        // (2) Cache maintenance CPU -> device over input and output: T_s2d
        auto t = clk::now();
        sync_region(0, 2 * bytes, "sync_for_device");
        const double s2d_us = us_since(t);

        // (3) Program and read back the arguments: T_cfg
        t = clk::now();
        for (int j = 0; j < 6; ++j) reg_write(offs[j], vals[j]);
        bool regs_ok = true;
        for (int j = 0; j < 6; ++j)
            if (reg_read(offs[j]) != vals[j]) regs_ok = false;
        const double cfg_us = us_since(t);

        if (!regs_ok) {
            std::fprintf(stderr, "ERROR: register read-back mismatch (check AFI_FS and loaded bitstream)\n");
            for (int j = 0; j < 6; ++j)
                std::fprintf(stderr, "  0x%02x wrote 0x%08x read 0x%08x\n",
                             offs[j], vals[j], reg_read(offs[j]));
            return 3;
        }

        // (4)-(5) Start and poll ap_done: T_kern
        t = clk::now();
        reg_write(REG_AP_CTRL, 0x1);
        bool done = false;
        while (true) {
            if (reg_read(REG_AP_CTRL) & 0x2) { done = true; break; }
            if (us_since(t) > KERNEL_TIMEOUT_US) break;
        }
        const double kern_us = us_since(t);

        if (!done) {
            std::fprintf(stderr, "ERROR: FPGA timeout (N=%ld K=%ld)\n", n, k);
            return 4;
        }

        // (6) Cache maintenance device -> CPU over the output: T_s2c
        t = clk::now();
        sync_region(bytes, bytes, "sync_for_cpu");
        const double s2c_us = us_since(t);
        __sync_synchronize();

        // (7) Verification (not timed)
        long errors = 0;
        for (long i = 0; i < n; ++i)
            if (output[i] != reference(static_cast<uint32_t>(i), k)) ++errors;

        std::printf("%s,%ld,%ld,%zu,%d,%.3f,%.3f,%.3f,%.3f,%.3f,%ld\n",
                    label, n, k, 2 * bytes, r,
                    cfg_us, s2d_us, kern_us, s2c_us,
                    cfg_us + s2d_us + kern_us + s2c_us, errors);
        std::fflush(stdout);

        if (errors != 0) {
            std::fprintf(stderr, "ERROR: %ld mismatching elements (N=%ld K=%ld)\n", errors, n, k);
            return 2;
        }
    }

    munmap(ctrl_map, CTRL_SIZE);
    close(mem_fd);
    munmap(buffer, buffer_size);
    close(buf_fd);
    return 0;
}
