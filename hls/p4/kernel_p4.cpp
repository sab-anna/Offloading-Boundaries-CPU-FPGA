#include <stdint.h>

extern "C" {

void microbench_p4(
    const uint32_t *in,
    uint32_t *out,
    int n,
    int k)
{
#pragma HLS INTERFACE m_axi port=in  offset=slave bundle=gmem0 depth=2048
#pragma HLS INTERFACE m_axi port=out offset=slave bundle=gmem1 depth=2048

#pragma HLS INTERFACE s_axilite port=in
#pragma HLS INTERFACE s_axilite port=out
#pragma HLS INTERFACE s_axilite port=n
#pragma HLS INTERFACE s_axilite port=k
#pragma HLS INTERFACE s_axilite port=return

    const int P = 4;

    for (int base = 0; base < n; base += P) {

        uint32_t x[P];
        uint32_t acc[P];

#pragma HLS ARRAY_PARTITION variable=x complete
#pragma HLS ARRAY_PARTITION variable=acc complete

        // Carica 4 elementi indipendenti
        for (int p = 0; p < P; ++p) {
#pragma HLS UNROLL

            if (base + p < n) {
                x[p] = in[base + p];
                acc[p] = 0;
            } else {
                x[p] = 0;
                acc[p] = 0;
            }
        }

        // Elabora i 4 elementi in parallelo
        for (int j = 0; j < k; ++j) {
#pragma HLS PIPELINE II=1

            for (int p = 0; p < P; ++p) {
#pragma HLS UNROLL

                uint32_t t =
                    (x[p] ^ (uint32_t)j) * 1664525u
                    + 1013904223u;

                acc[p] += t;
            }
        }

        // Scrive i 4 risultati
        for (int p = 0; p < P; ++p) {
#pragma HLS UNROLL

            if (base + p < n) {
                out[base + p] = acc[p];
            }
        }
    }
}

}