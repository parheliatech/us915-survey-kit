"""Run every decoder on one raw-IQ dwell and leave one result file per decoder next to the dwell's meta file.

Usage: python3 tools/process_dwell.py DWELL_DIR/NAME.meta.json [--iq FILE.cu8] [--ppm P] [--keep-iq] [--fsk-seconds 60]

Steps (each skipped when its output already exists, so a rerun after a crash or power cut continues):
  NAME.clip.json        ADC level and clipping statistics (receiver health)
  NAME.det.json         LoRa preambles, 62.5/125/250/500 kHz x SF7-12 (lora_detect)
  NAME.lora.jsonl       decoded LoRa frames incl. Class B beacons (lora_decode; crash-safe, resumes per detection)
  NAME.wisun.jsonl      IEEE 802.15.4g SUN-FSK 50 kbps frame headers (wisun_decode)
  NAME.fsk.json         narrow FSK bursts in the first --fsk-seconds (fsk_survey)
  NAME.done             written last; the IQ file is then deleted unless --keep-iq
Decoders run as child processes, so a crash in one (gr-lora_sdr can segfault) does not take the others down.
"""
import argparse, json, os, subprocess, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
FS = 2_400_000


def clip_stats(path):
    m = np.memmap(path, dtype=np.uint8, mode='r')
    clip = tot = 0
    peaks, rms = [], []
    for k in range(20):  # 20 x 0.5 s slices spread over the dwell
        s = int(k * m.size / 20) // 2 * 2
        x = np.asarray(m[s:s + FS]).astype(np.int16) - 127
        if not x.size:
            continue
        a = np.abs(x)
        clip += int((a >= 127).sum()); tot += a.size
        peaks.append(int(a.max())); rms.append(float(np.sqrt((x.astype(np.float32) ** 2).mean())))
    return dict(clip_ppm=round(1e6 * clip / max(tot, 1), 2), peak=max(peaks or [0]), rms=round(float(np.median(rms or [0])), 2))


def run(cmd, log):
    with open(log, 'a') as f:
        f.write('$ ' + ' '.join(cmd) + '\n'); f.flush()
        return subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT).returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('meta'); ap.add_argument('--iq'); ap.add_argument('--ppm', type=float, default=0.0)
    ap.add_argument('--keep-iq', action='store_true'); ap.add_argument('--fsk-seconds', type=float, default=60)
    a = ap.parse_args()
    meta = json.load(open(a.meta))
    base = a.meta[:-len('.meta.json')]
    iq = a.iq or meta['iq']
    if os.path.exists(base + '.done'):
        return 0
    if not os.path.exists(iq):
        print(f'{iq}: IQ file missing, cannot process', file=sys.stderr)
        return 2
    log = base + '.process.log'
    fc, py = str(meta['fc']), sys.executable
    fails = []

    if not os.path.exists(base + '.clip.json'):
        json.dump(clip_stats(iq), open(base + '.clip.json.tmp', 'w'))
        os.replace(base + '.clip.json.tmp', base + '.clip.json')

    if not os.path.exists(base + '.det.json'):
        if run([py, os.path.join(HERE, 'lora_detect.py'), iq, '--fc', fc, '--fs', str(FS), '--json', base + '.det.json.tmp'], log) == 0:
            os.replace(base + '.det.json.tmp', base + '.det.json')
        else:
            fails.append('lora_detect')

    if os.path.exists(base + '.det.json') and not os.path.exists(base + '.lora.ok'):
        cmd = [py, os.path.join(HERE, 'lora_decode.py'), iq, '--fc', fc, '--fs', str(FS), '--det', base + '.det.json',
               '--out', base + '.lora.jsonl', '--ppm', str(a.ppm), '--t0', str(meta['t0'])]
        for _ in range(10):  # each crash skips the detection it died on; give up after 10
            if run(cmd, log) == 0:
                open(base + '.lora.ok', 'w').close()
                break
        else:
            fails.append('lora_decode')

    if not os.path.exists(base + '.wisun.jsonl'):
        tmp = base + '.wisun.jsonl.tmp'
        if os.path.exists(tmp):
            os.remove(tmp)
        if run([py, os.path.join(HERE, 'wisun_decode.py'), iq, '--fc', fc, '--fs', str(FS), '--ppm', str(a.ppm), '--out', tmp], log) == 0:
            os.replace(tmp, base + '.wisun.jsonl')
        else:
            fails.append('wisun_decode')

    if a.fsk_seconds > 0 and not os.path.exists(base + '.fsk.json'):
        if run([py, os.path.join(HERE, 'fsk_survey.py'), iq, '--fc', fc, '--fs', str(FS), '--max-s', str(a.fsk_seconds),
                '--json', base + '.fsk.json.tmp'], log) == 0:
            os.replace(base + '.fsk.json.tmp', base + '.fsk.json')
        else:
            fails.append('fsk_survey')

    if fails:
        print(f'{os.path.basename(base)}: failed: {", ".join(fails)} (see {log})', file=sys.stderr)
        return 1
    open(base + '.done', 'w').close()
    if not a.keep_iq:
        os.remove(iq)
    print(f'{os.path.basename(base)}: processed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
