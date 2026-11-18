#include <cstdint>
#include <iostream>

extern "C" {

void microbench_p4(
    const uint32_t *in,
    uint32_t *out,
    int n,
    int k);

}

static uint32_t reference(uint32_t x, int k)
{
    uint32_t acc = 0;

    for (int j = 0; j < k; ++j) {
        uint32_t t =
            (x ^ static_cast<uint32_t>(j))
            * 1664525u
            + 1013904223u;

        acc += t;
    }

    return acc;
}

int main()
{
    constexpr int N = 1027;
    constexpr int K = 16;

    uint32_t in[N];
    uint32_t out[N];

    for (int i = 0; i < N; ++i) {
        in[i] = static_cast<uint32_t>(i);
        out[i] = 0xDEADBEEF;
    }

    microbench_p4(in, out, N, K);

    int errors = 0;

    for (int i = 0; i < N; ++i) {
        uint32_t expected = reference(in[i], K);

        if (out[i] != expected) {
            if (errors < 10) {
                std::cout
                    << "ERROR i=" << i
                    << " got=0x" << std::hex << out[i]
                    << " expected=0x" << expected
                    << std::dec << "\n";
            }

            ++errors;
        }
    }

    if (errors == 0) {
        std::cout << "microbench_p4 TEST PASSED\n";
        return 0;
    }

    std::cout
        << "microbench_p4 TEST FAILED: "
        << errors << " errors\n";

    return 1;
}