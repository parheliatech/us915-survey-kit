"""Make a synthetic RTL-style .cu8 window containing known LoRa packets, for testing lora_detect / lora_decode.

Usage: . lib/env.sh && python3 tools/synth_window.py OUT.cu8 [--snr-db 0]
Writes OUT.cu8 (fs 2.4 MS/s, fc 904.6 MHz, 6 s) and OUT.truth.json. Noise RMS ~4 ADC codes, like the real captures.
"""
import argparse, json, sys, os
import numpy as np
from scipy.signal import resample_poly
sys.path.insert(0, os.path.dirname(__file__))
from gnuradio import gr, blocks
from gnuradio import lora_sdr

FS, FC, DUR = 2_400_000, 904.6e6, 6.0
# (t_start s, f Hz, bw, sf, sync, cr, payload)
PACKETS = [
    (0.20, 904.1e6, 125000, 7, 0x34, 1, b"lorawan-like sf7"),
    (0.50, 905.3e6, 125000, 10, 0x34, 1, b"lorawan-like sf10"),
    (1.40, 904.375e6, 250000, 11, 0x2B, 4, b"mesh-like sf11/250"),
    (2.90, 904.6e6, 500000, 8, 0x34, 1, b"500k sf8 at dc"),
    (3.30, 903.9e6, 125000, 12, 0x12, 1, b"private sf12"),
    (5.00, 905.0e6, 250000, 9, 0x2B, 1, b"sf9/250"),
    (5.02, 903.8e6, 125000, 8, 0x34, 2, b"overlaps in time"),
]


def modulate(payload, sf, bw, sync, cr, os_=4):
    tb = gr.top_block()
    src = blocks.vector_source_b(list(payload + b","), False)
    chain = [src, lora_sdr.whitening(False, False, ',', 'packet_len'), lora_sdr.header(False, True, cr),
             lora_sdr.add_crc(True), lora_sdr.hamming_enc(cr, sf), lora_sdr.interleaver(cr, sf, 2, bw),
             lora_sdr.gray_demap(sf), lora_sdr.modulate(sf, bw * os_, bw, [sync], 2 ** sf * os_, 8)]
    snk = blocks.vector_sink_c()
    tb.connect(*chain, snk)
    tb.run()
    return np.array(snk.data(), dtype=np.complex64), bw * os_


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('out'); ap.add_argument('--snr-db', type=float, default=0.0)
    a = ap.parse_args()
    rng = np.random.default_rng(1)
    n = int(FS * DUR)
    noise_rms = 4 / 127.5
    x = (rng.normal(size=n) + 1j * rng.normal(size=n)).astype(np.complex64) * noise_rms / np.sqrt(2)
    truth = []
    for t, f, bw, sf, sync, cr, pl in PACKETS:
        s, rate = modulate(pl, sf, bw, sync, cr)
        g = np.gcd(int(FS), int(rate))
        s = resample_poly(s, FS // g, rate // g).astype(np.complex64)
        # in-channel SNR target: noise power in bw is noise_rms^2 * bw/FS
        amp = np.sqrt(noise_rms ** 2 * bw / FS * 10 ** (a.snr_db / 10))
        k = np.arange(len(s))
        s = s * amp * np.exp(2j * np.pi * (f - FC) / FS * k)
        i0 = int(t * FS)
        x[i0:i0 + len(s)] += s[:n - i0]
        truth.append(dict(t=t, f=f, bw=bw, sf=sf, sync=sync, cr=cr, payload=pl.decode(), dur=len(s) / FS))
    q = np.clip(np.round(np.stack([x.real, x.imag], 1).ravel() * 127.5 + 127.5), 0, 255).astype(np.uint8)
    q.tofile(a.out)
    json.dump(truth, open(a.out.rsplit('.', 1)[0] + '.truth.json', 'w'), indent=0)
    print(f"wrote {a.out} ({n} samples), {len(truth)} packets, in-channel SNR {a.snr_db} dB")


if __name__ == '__main__':
    main()
