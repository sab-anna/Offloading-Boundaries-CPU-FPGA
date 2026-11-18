#include <stdint.h>

extern "C" {

void microbench(
    const uint32_t *in,
    uint32_t *out,
    int n,
    int k)
{
#pragma HLS INTERFACE m_axi port=in  offset=slave bundle=gmem0
#pragma HLS INTERFACE m_axi port=out offset=slave bundle=gmem1

#pragma HLS INTERFACE s_axilite port=in
#pragma HLS INTERFACE s_axilite port=out
#pragma HLS INTERFACE s_axilite port=n
#pragma HLS INTERFACE s_axilite port=k
#pragma HLS INTERFACE s_axilite port=return

    for (int i = 0; i < n; ++i) {

        uint32_t x = in[i];
        uint32_t acc = 0;

        for (int j = 0; j < k; ++j) {
#pragma HLS PIPELINE II=1

            uint32_t t =
                (x ^ (uint32_t)j) * 1664525u
                + 1013904223u;

            acc += t;
        }

        out[i] = acc;
    }
}

}