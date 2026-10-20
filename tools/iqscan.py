"""Rotate raw-IQ dwells evenly across 902-928 MHz and process each one with every decoder (all networks, equal priority).

Usage: python3 tools/iqscan.py RUN_DIR --iq-dir DIR --passes 12 [--dwell 240] [--gain 40] [--ppm P] [--keep-iq]

15 windows of 2.4 MS/s (centres 902.9-927.1 MHz, ~1.73 MHz apart, so they overlap and cover 901.7-928.3 MHz) are
recorded in order; one pass of all 15 is one visit to every window. After each capture, process_dwell.py runs in the
background (LoRa, 802.15.4g, FSK, receiver health) and deletes the IQ unless --keep-iq. Capture never waits for the
decoders: dwells queue up in IQ_DIR (up to --iq-cap-gb) and at most --max-parallel decode at once; the queue is
finished after the last capture, so a slow PC only makes the run longer, not the time coverage uneven.
Resumable: a dwell is identified by (pass, window); rerunning the same command records only the missing ones.
Capture waits only while IQ_DIR holds more than --iq-cap-gb or its disk has less than --min-free-gb free. Create RUN_DIR/STOP to stop cleanly after the current dwell.
"""
import argparse, glob, json, os, re, shutil, subprocess, sys, time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
FS = 2_400_000
WINDOWS = [round(902.9e6 + i * (927.1e6 - 902.9e6) / 14, -3) for i in range(15)]


def dir_gb(path):
    return sum(os.path.getsize(f) for f in glob.glob(os.path.join(path, '*.cu8'))) / 1e9


def log(run_dir, msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(os.path.join(run_dir, 'scan.log'), 'a') as f:
        f.write(line + '\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('run_dir'); ap.add_argument('--iq-dir', required=True)
    ap.add_argument('--passes', type=int, required=True); ap.add_argument('--dwell', type=float, default=240)
    ap.add_argument('--gain', type=float, default=40); ap.add_argument('--ppm', type=float, default=0.0)
    ap.add_argument('--keep-iq', action='store_true'); ap.add_argument('--iq-cap-gb', type=float, default=80)
    ap.add_argument('--min-free-gb', type=float, default=5)
    ap.add_argument('--max-parallel', type=int, default=2); ap.add_argument('--fsk-seconds', type=float, default=60)
    ap.add_argument('--device', default='0')
    a = ap.parse_args()
    dwell_dir = os.path.join(a.run_dir, 'dwells')
    os.makedirs(dwell_dir, exist_ok=True); os.makedirs(a.iq_dir, exist_ok=True)
    have = set()
    for m in glob.glob(os.path.join(dwell_dir, '*.meta.json')):
        d = json.load(open(m))
        if d.get('samples', 0) >= 0.9 * d.get('expected', 1):
            have.add((d['pass'], d['window']))
    log(a.run_dir, f"start: {a.passes} passes x {len(WINDOWS)} windows {WINDOWS[0]/1e6:.3f}-{WINDOWS[-1]/1e6:.3f} MHz, "
                   f"dwell {a.dwell:g} s, gain {a.gain:g}, ppm {a.ppm:g}; {len(have)} dwells already recorded")
    stop_file = os.path.join(a.run_dir, 'STOP')
    plog = open(os.path.join(a.run_dir, 'process.log'), 'a')
    queue, running = [], []
    # dwells recorded before a restart but not yet decoded go first
    known = set()
    for m in sorted(glob.glob(os.path.join(dwell_dir, '*.meta.json'))):
        d = json.load(open(m))
        known.add(os.path.abspath(d['iq']))
        if not os.path.exists(m[:-len('.meta.json')] + '.done') and os.path.exists(d['iq']):
            queue.append(m)
    # a capture cut off by a crash or power cut leaves a partial IQ file and no meta: delete it (it is re-recorded)
    for f in glob.glob(os.path.join(a.iq_dir, 'w*k_*Z.cu8')):
        if re.fullmatch(r'w\d+k_\d{8}T\d{6}Z\.cu8', os.path.basename(f)) and os.path.abspath(f) not in known:
            os.remove(f)
            log(a.run_dir, f'removed partial capture {os.path.basename(f)}')

    def service():
        """Start queued dwells while fewer than --max-parallel decoders run. Capture never waits for this."""
        running[:] = [q for q in running if q.poll() is None]
        while queue and len(running) < a.max_parallel:
            m = queue.pop(0)
            cmd = [sys.executable, os.path.join(HERE, 'process_dwell.py'), m, '--ppm', str(a.ppm),
                   '--fsk-seconds', str(a.fsk_seconds)] + (['--keep-iq'] if a.keep_iq else [])
            running.append(subprocess.Popen(['nice'] + cmd, stdout=plog, stderr=subprocess.STDOUT))

    for p in range(a.passes):
        for i, fc in enumerate(WINDOWS):
            if (p, i) in have:
                continue
            while True:  # capture waits only for disk space
                service()
                if os.path.exists(stop_file):
                    break
                if dir_gb(a.iq_dir) < a.iq_cap_gb and shutil.disk_usage(a.iq_dir).free > a.min_free_gb * 1e9:
                    break
                time.sleep(10)
            if os.path.exists(stop_file):
                log(a.run_dir, 'STOP file found: stopping capture')
                break
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            name = f"w{int(fc/1e3)}k_{stamp}"
            iq = os.path.join(a.iq_dir, name + '.cu8')
            t0 = time.time()
            rp = subprocess.Popen(['rtl_sdr', '-d', a.device, '-f', str(int(fc)), '-s', str(FS), '-g', str(a.gain),
                                   '-n', str(int(a.dwell * FS)), iq], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            while rp.poll() is None:  # keep the decoders fed while recording
                service()
                time.sleep(2)
            err = rp.stderr.read()
            got = os.path.getsize(iq) // 2 if os.path.exists(iq) else 0
            lost = [l for l in err.splitlines() if 'lost' in l.lower()]
            meta = {'name': name, 'iq': iq, 'window': i, 'pass': p, 'fc': fc, 't0': t0, 'samples': got,
                    'expected': int(a.dwell * FS), 'rc': rp.returncode, 'lost': lost, 'gain': a.gain, 'ppm': a.ppm}
            mpath = os.path.join(dwell_dir, name + '.meta.json')
            json.dump(meta, open(mpath, 'w'))
            if got < 0.9 * a.dwell * FS:
                log(a.run_dir, f"WARN {name}: short capture {got} samples rc={rp.returncode} {err.strip()[-200:]}")
                if os.path.exists(iq):
                    os.remove(iq)
                os.rename(mpath, mpath + '.short')
                time.sleep(5)
                continue
            queue.append(mpath)
            service()
            log(a.run_dir, f"pass {p + 1}/{a.passes} win {i:2d} {fc/1e6:8.3f} MHz captured {got/FS:6.1f} s"
                           f"{' LOST ' + str(lost) if lost else ''}; decoding {len(running)}, queued {len(queue)}")
        else:
            continue
        break
    log(a.run_dir, f'capture finished; decoding the remaining {len(queue) + len(running)} dwells')
    while queue or running:
        service()
        time.sleep(5)
    recorded = len({(json.load(open(m))['pass'], json.load(open(m))['window']) for m in glob.glob(os.path.join(dwell_dir, '*.meta.json'))})
    complete = recorded >= a.passes * len(WINDOWS)
    log(a.run_dir, f"done: {recorded} of {a.passes * len(WINDOWS)} dwells recorded")
    sys.exit(0 if complete else 3)


if __name__ == '__main__':
    main()
