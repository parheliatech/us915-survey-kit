"""Find narrowband (non-LoRa) bursts in an RTL-SDR .cu8 window and characterise them as FSK: tone deviation,
symbol rate, and hard-decision bits (for preamble / sync-word spotting). Protocol-agnostic.

Usage: python3 tools/fsk_survey.py FILE.cu8 --fc HZ --fs HZ [--json out.json] [--max-s 60]

Detection: 256-pt STFT; each frame is referenced to its own median (so broadband floor rises don't count); a burst is
a connected region >= THRESH dB above that, <= MAX_BW wide and >= MIN_MS long.
Demod: mix the burst to its centre, FFT brick-wall filter to its occupied width, instantaneous frequency from the
phase difference. Symbol rate from the spectral line of |d(sign)| (transition rate), then bits by sampling at
symbol centres. Bits are NOT written to the json (only rate, deviation, preamble/sync guesses and a bit count).
"""
import argparse, json
import numpy as np
from scipy import ndimage

NFFT = 256
THRESH = 12.0
MAX_BW = 400e3
MIN_MS = 1.0


def load_cu8(path, start, count):
    raw = np.fromfile(path, dtype=np.uint8, offset=2 * start, count=2 * count)
    x = ((raw.astype(np.float32) - 127.5) / 127.5).view(np.complex64)
    return x - x.mean()


def find(path, fs, chunk_s=5.0, max_s=None):
    total = np.memmap(path, dtype=np.uint8, mode='r').size // 2
    if max_s:
        total = min(total, int(max_s * fs))
    chunk = int(chunk_s * fs) // NFFT * NFFT
    edge = NFFT // 16
    out = []
    for s0 in range(0, total, chunk):
        x = load_cu8(path, s0, min(chunk, total - s0))
        n = len(x) // NFFT
        fr = x[:n * NFFT].reshape(n, NFFT) * np.hanning(NFFT).astype(np.float32)
        P = 10 * np.log10(np.abs(np.fft.fftshift(np.fft.fft(fr, axis=1), axes=1)) ** 2 + 1e-12)
        P -= np.median(P, axis=1, keepdims=True)
        P[:, :edge] = 0; P[:, -edge:] = 0; P[:, NFFT // 2 - 1:NFFT // 2 + 2] = 0
        mask = ndimage.binary_closing(P > THRESH, structure=np.ones((3, 3)))
        lab, _ = ndimage.label(mask)
        for i, (ts, fsl) in enumerate(ndimage.find_objects(lab)):
            dur = (ts.stop - ts.start) * NFFT / fs
            bw = (fsl.stop - fsl.start) * fs / NFFT
            if dur * 1e3 < MIN_MS or bw > MAX_BW:
                continue
            reg = np.where(lab[ts, fsl] == i + 1, P[ts, fsl], -np.inf)
            out.append(dict(s0=s0 + ts.start * NFFT, n=(ts.stop - ts.start) * NFFT,
                            f_off=((fsl.start + fsl.stop - 1) / 2 - NFFT // 2) * fs / NFFT, bw=bw,
                            peak_db=float(reg.max())))
    return out


def demod(path, fs, b):
    pad = int(0.0005 * fs)
    s0 = max(0, b['s0'] - pad)
    x = load_cu8(path, s0, b['n'] + 2 * pad)
    x = x * np.exp(-2j * np.pi * b['f_off'] / fs * np.arange(len(x))).astype(np.complex64)
    X = np.fft.fft(x)
    f = np.fft.fftfreq(len(x), 1 / fs)
    X[np.abs(f) > b['bw'] / 2 + 20e3] = 0
    y = np.fft.ifft(X)
    amp = np.abs(y)
    on = amp > 0.5 * np.percentile(amp, 90)
    inst = np.angle(y[1:] * np.conj(y[:-1])) * fs / (2 * np.pi)
    inst = inst[on[1:]]
    if len(inst) < 200:
        return None
    lo, hi = np.percentile(inst, [10, 90])
    centre = (lo + hi) / 2
    dev = (hi - lo) / 2
    s = np.sign(ndimage.uniform_filter1d(inst - centre, 5))
    # transition rate spectral line -> symbol rate
    tr = np.abs(np.diff(s)) / 2
    nfft = 1 << max(16, int(np.ceil(np.log2(len(tr)))) + 1)  # >= 2x zero-padding, <= 37 Hz bins at 2.4 MS/s
    F = np.abs(np.fft.rfft(tr - tr.mean(), n=nfft))
    fr = np.fft.rfftfreq(nfft, 1 / fs)
    band = (fr > 1e3) & (fr < 300e3)
    baud = fr[band][F[band].argmax()]
    # bit sampling: phase from the strongest line
    sps = fs / baud
    k = np.arange(len(tr))
    ph = np.angle(np.sum(tr * np.exp(-2j * np.pi * k / sps)))
    t0 = (ph / (2 * np.pi)) * sps + sps / 2  # transitions at phase 0 -> sample half a symbol later
    idx = (t0 % sps + np.arange(0, len(s), sps)).astype(int)
    idx = idx[idx < len(s)]
    bits = (s[idx] > 0).astype(np.uint8)
    return dict(f_centre_off=b['f_off'] + centre, dev_hz=round(float(dev)), baud=round(float(baud)), nbits=len(bits),
                line_snr=round(float(F[band].max() / np.median(F[band])), 1)), bits


def preamble_sync(bits):
    """Length of the leading 0101.. run and the 16/32 bits that follow it (common sync-word position)."""
    alt = np.flatnonzero(np.diff(bits) == 0)
    # longest alternating run
    edges = np.concatenate([[-1], alt, [len(bits) - 1]])
    runs = np.diff(edges)
    j = runs.argmax()
    start, ln = edges[j] + 1, runs[j]
    after = bits[start + ln: start + ln + 32]
    return int(start), int(ln), ''.join(map(str, after))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('file'); ap.add_argument('--fc', type=float, required=True); ap.add_argument('--fs', type=float, required=True)
    ap.add_argument('--json'); ap.add_argument('--max-s', type=float)
    a = ap.parse_args()
    bursts = find(a.file, a.fs, max_s=a.max_s)
    print(f"{len(bursts)} narrow bursts")
    recs = []
    for b in bursts:
        r = demod(a.file, a.fs, b)
        if not r:
            continue
        info, bits = r
        pst, pln, after = preamble_sync(bits)
        rec = dict(t=round(b['s0'] / a.fs, 4), dur_ms=round(b['n'] / a.fs * 1e3, 2),
                   f=round(a.fc + info['f_centre_off']), bw_occ=b['bw'], peak_db=round(b['peak_db'], 1),
                   **{k: v for k, v in info.items() if k != 'f_centre_off'},
                   pre_start=pst, pre_len=pln, after_pre=after)
        recs.append(rec)
        print(f"t={rec['t']:8.3f} {rec['dur_ms']:6.1f}ms f={rec['f']/1e6:9.4f} dev={rec['dev_hz']/1e3:5.1f}k "
              f"baud={rec['baud']/1e3:6.2f}k line={rec['line_snr']:5.1f} bits={rec['nbits']:4d} pre@{pst} len {pln} -> {after}")
    if a.json:
        json.dump(recs, open(a.json, 'w'), indent=0)


if __name__ == '__main__':
    main()
