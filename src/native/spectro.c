/* SPDX-License-Identifier: GPL-3.0-or-later
 * D36: small scalar PCM/window/band/code loops. The FFT remains libgstfft.
 * No allocation, threads, I/O or retained audio in this ABI. */
#include <math.h>
#include <stddef.h>
#include <stdint.h>

typedef struct { float real, imaginary; } Complex;
int cassini_spectro_abi(void) { return 1; }

int cassini_spectro_mono(const unsigned char *pcm, size_t frames,
                        unsigned channels, float *out) {
    if (!pcm || !out || (channels != 1 && channels != 2)) return -1;
    for (size_t i = 0; i < frames; i++) {
        const unsigned char *p = pcm + i * channels * 2;
        int32_t a = (int16_t)((uint16_t)p[0] | (uint16_t)p[1] << 8);
        if (channels == 2) {
            int32_t b = (int16_t)((uint16_t)p[2] | (uint16_t)p[3] << 8);
            out[i] = (float)(a + b) * 0.5f;
        } else out[i] = (float)a;
    }
    return 0;
}

int cassini_spectro_window(float *frame, const float *window, unsigned n) {
    if (!frame || !window || n < 256 || n > 8192) return -1;
    double mean = 0;
    for (unsigned i = 0; i < n; i++) mean += frame[i];
    mean /= n;
    for (unsigned i = 0; i < n; i++) frame[i] = (float)(frame[i] - mean) * window[i];
    return 0;
}

int cassini_spectro_powers(const Complex *fft, unsigned n,
                          const unsigned *first, const unsigned *last,
                          const double *lo, const double *hi, double scale,
                          double *out) {
    if (!fft || !first || !last || !lo || !hi || !out || n < 256 || n > 8192) return -1;
    for (unsigned band = 0; band < 32; band++) {
        unsigned a = first[band], b = last[band];
        if (a > b || b > n / 2) return -1;
        double sum = 0;
        for (unsigned k = a; k <= b; k++) {
            double re = fft[k].real, im = fft[k].imaginary;
            double weight = k == a ? lo[band] : (k == b ? hi[band] : 1);
            sum += (re * re + im * im) * weight;
        }
        out[band] = sum * scale;
    }
    return 0;
}

int cassini_spectro_codes(const double *power, double *held,
                         int continuous, double release, unsigned char *out) {
    if (!power || !held || !out || release < 0) return -1;
    for (unsigned i = 0; i < 32; i++) {
        if (!isfinite(power[i]) || power[i] < 0) return -1;
        double db = 10 * log10(fmax(power[i], 1e-12));
        if (continuous) db = fmax(db, held[i] - release);
        held[i] = db;
        double code = 255 * fmin(1, fmax(0, (db + 80) / 60));
        double rounded = floor(code);
        double part = code - rounded;
        if (part > 0.5 || (part == 0.5 && ((unsigned)rounded & 1))) rounded++;
        out[i] = (unsigned char)rounded;
    }
    return 0;
}
