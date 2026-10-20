"""Write the survey report from RUN_DIR/summary.json: RUN_DIR/report/report.html (self-contained, charts embedded)
and RUN_DIR/report/report.md (+ chart PNGs beside it).

Usage: python3 tools/report.py RUN_DIR

Every sentence is generated from this run's measurements; nothing is carried over from another site. Protocol names
are only used where a decoder confirmed them (LoRa sync word + header structure, 802.15.4g SFD + frame check).
"""
import base64, html, io, json, os, sys
from datetime import datetime

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

BLUE, ORANGE, GREEN, NAVY, GREY, LIGHT = '#2a6fdb', '#e8692e', '#1a9e6e', '#0d2f5e', '#9a9a8e', '#d0d0c8'
LORA_COLOURS = {'Meshtastic': ORANGE, 'MeshCore': GREEN, 'LoRaWAN': NAVY}


class Doc:
    def __init__(self):
        self.blocks = []

    def h(self, level, text): self.blocks.append(('h', level, text))
    def p(self, text): self.blocks.append(('p', text))
    def bullets(self, items): self.blocks.append(('ul', items))
    def table(self, head, rows): self.blocks.append(('table', head, rows))
    def img(self, name, fig, caption): self.blocks.append(('img', name, fig, caption))

    def write(self, out_dir, title):
        os.makedirs(out_dir, exist_ok=True)
        md, body = [], []
        for b in self.blocks:
            if b[0] == 'h':
                md.append('#' * b[1] + ' ' + b[2]); body.append(f'<h{b[1]}>{html.escape(b[2])}</h{b[1]}>')
            elif b[0] == 'p':
                md.append(b[1]); body.append(f'<p>{html.escape(b[1])}</p>')
            elif b[0] == 'ul':
                md.append('\n'.join('- ' + x for x in b[1])); body.append('<ul>' + ''.join(f'<li>{html.escape(x)}</li>' for x in b[1]) + '</ul>')
            elif b[0] == 'table':
                head, rows = b[1], b[2]
                md.append('| ' + ' | '.join(head) + ' |\n|' + '---|' * len(head) + '\n' +
                          '\n'.join('| ' + ' | '.join(str(c).replace('|', '/') for c in r) + ' |' for r in rows))
                body.append('<div class="tw"><table><thead><tr>' + ''.join(f'<th>{html.escape(h)}</th>' for h in head) + '</tr></thead><tbody>' +
                            ''.join('<tr>' + ''.join(f'<td>{html.escape(str(c))}</td>' for c in r) + '</tr>' for r in rows) + '</tbody></table></div>')
            elif b[0] == 'img':
                name, fig, cap = b[1], b[2], b[3]
                fig.savefig(os.path.join(out_dir, name), dpi=120, bbox_inches='tight')
                buf = io.BytesIO(); fig.savefig(buf, format='png', dpi=120, bbox_inches='tight'); plt.close(fig)
                md.append(f'![{cap}]({name})\n\n*{cap}*')
                body.append(f'<figure><img alt="{html.escape(cap)}" src="data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}">'
                            f'<figcaption>{html.escape(cap)}</figcaption></figure>')
            md.append('')
        open(os.path.join(out_dir, 'report.md'), 'w').write('\n'.join(md))
        css = ('body{font:15px/1.5 system-ui,sans-serif;max-width:980px;margin:0 auto;padding:16px;color:#1d1d1b;background:#fff}'
               'h1{font-size:26px}h2{margin-top:36px;border-bottom:1px solid #ddd;padding-bottom:4px}h3{margin-top:24px}'
               '.tw{overflow-x:auto}table{border-collapse:collapse;margin:12px 0;font-size:13px}'
               'th,td{border:1px solid #ddd;padding:4px 8px;text-align:left;vertical-align:top}th{background:#f4f4f1}'
               'figure{margin:16px 0}img{max-width:100%}figcaption{color:#666;font-size:12px}'
               '@media (prefers-color-scheme:dark){body{background:#1b1b1a;color:#e8e8e4}th{background:#2a2a28}th,td{border-color:#444}'
               'img{background:#fff}}')
        open(os.path.join(out_dir, 'report.html'), 'w').write(
            f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)}</title><style>{css}</style></head><body>' + '\n'.join(body) + '</body></html>')


def n(x):
    return f'{x:,}' if isinstance(x, int) else str(x)


def pct(a, b):
    return f'{100 * a / b:.0f}%' if b else 'n/a'


def pl(k, word, plural=None):
    return f"{n(k)} {word if k == 1 else (plural or word + 's')}"


def bwk(bw):
    return f'{bw / 1000:g} kHz'


def hhmm(ts):
    return datetime.fromtimestamp(ts).strftime('%H:%M')


def lora_colour(label):
    for k, c in LORA_COLOURS.items():
        if label.startswith(k):
            return c
    return GREY


# ---------------------------------------------------------------- charts
def band_map(S):
    sun, lora, fsk, power = S.get('sunfsk'), S.get('lora') or {}, S.get('fsk') or {}, S.get('power') or {}
    lanes = []
    for c in lora.get('classes', []):
        if c['crc_ok'] or c['header_ok'] >= 3:
            half = max(c['bw'] / 2e6, 0.03)
            lanes.append((f"{c['label']} ({c['crc_ok']} valid)", c['mhz'] - half, c['mhz'] + half, lora_colour(c['label']), 1.0))
    for u in lora.get('undecoded', [])[:4]:
        if u['detections'] >= 3 and not u['same_slope_as'] and not u['same_mode_as']:
            lanes.append((f"Undecoded LoRa {bwk(u['bw'])}/SF{u['sf']} ({u['detections']} det.)", u['mhz'] - u['bw'] / 2e6, u['mhz'] + u['bw'] / 2e6, GREY, 0.6))
    for f in fsk.get('families', []):
        if not f['sun_fsk']:
            lanes.append((f"FSK {f['name']} {f['baud'] / 1000:g} kbaud ({f['bursts']})", f['mhz_min'], f['mhz_max'], GREY, 0.45))
    for c in power.get('narrowband', [])[:3]:
        lanes.append((f"Carrier {c['mhz']:.3f} MHz (power survey)", c['mhz'] - 0.02, c['mhz'] + 0.02, GREY, 0.8))
    has_bars = bool(sun and sun['per_channel'])
    fig, axes = plt.subplots(2 if has_bars else 1, 1, figsize=(10, 2.2 + 0.32 * len(lanes) + (2.2 if has_bars else 0)), sharex=True,
                             gridspec_kw=dict(height_ratios=[2.2, max(1, 0.32 * len(lanes))]) if has_bars else None)
    axes = axes if has_bars else [axes]
    if has_bars:
        ax = axes[0]
        xs = [c[0] for c in sun['per_channel']]; ys = [c[1] for c in sun['per_channel']]
        ax.bar(xs, ys, width=0.15, color=BLUE)
        ax.set_ylabel('valid frames\nper channel'); ax.set_title('802.15.4g SUN-FSK frames with a valid checksum, per 200 kHz channel', fontsize=10, loc='left')
        ax.spines[['top', 'right']].set_visible(False)
    ax = axes[-1]
    for i, (name, lo, hi, col, alpha) in enumerate(lanes):
        y = len(lanes) - 1 - i
        ax.barh(y, max(hi - lo, 0.05), left=lo, height=0.6, color=col, alpha=alpha)
        ax.text(901.5, y, name, ha='right', va='center', fontsize=8)
    ax.set_yticks([]); ax.set_xlim(901.6, 928.4); ax.set_xlabel('MHz (corrected)')
    ax.spines[['top', 'right', 'left']].set_visible(False)
    if not lanes:
        ax.text(915, 0, 'no other signal classes found', ha='center', fontsize=9)
    fig.tight_layout()
    return fig


def time_of_day(S):
    sun, lora = S.get('sunfsk'), S.get('lora') or {}
    cov = S['coverage']
    series = []
    if sun and sun['per_visit']:
        wv = cov['window_visits']  # [window, t0, mhz, pass, decoded]
        ok_by = dict((int(k), v) for k, v in sun['per_visit'])
        dwell_min = cov['dwell_s'] / 60 or 4
        for name, sel in (('below 916 MHz', lambda m: m < 916), ('above 916 MHz', lambda m: m >= 916)):
            pts = {}
            for w, t0, mhz, pas, decoded in wv:
                if sel(mhz) and decoded:
                    a = pts.setdefault(pas, [0, 0, []]); a[0] += ok_by.get(t0, 0); a[1] += 1; a[2].append(t0)
            xs = [sum(v[2]) / len(v[2]) for k, v in sorted(pts.items())]
            ys = [v[0] / (v[1] * dwell_min) for k, v in sorted(pts.items())]
            if xs:
                series.append(('line', f'802.15.4g valid frames / min / window, {name}', xs, ys))
    for c in lora.get('classes', []):
        if c['crc_ok'] >= 5:
            series.append(('bar', f"{c['label']} {c['mhz']:.3f} MHz: valid frames per visit", [v for v, _ in c['per_visit']], [k for _, k in c['per_visit']], lora_colour(c['label'])))
    if not series:
        return None
    fig, axes = plt.subplots(len(series), 1, figsize=(10, 1.7 * len(series) + 0.6), sharex=True, squeeze=False)
    for ax, s in zip(axes[:, 0], series):
        if s[0] == 'line':
            ax.plot(s[2], s[3], '-o', ms=3, color=BLUE)
        else:
            ax.bar(s[2], s[3], width=200, color=s[4])
        ax.set_title(s[1], fontsize=9, loc='left'); ax.spines[['top', 'right']].set_visible(False)
    ticks = axes[-1, 0].get_xticks()
    axes[-1, 0].set_xticks(ticks); axes[-1, 0].set_xticklabels([hhmm(t) for t in ticks], fontsize=8)
    axes[-1, 0].set_xlabel('local time')
    fig.tight_layout()
    return fig


def power_hourly(P):
    rows = [(g['name'], g['hourly']) for g in P['groups']] + [('Whole band (any bin)', P['band_hourly']), ('Wideband events (>= 40 bins)', P['wideband']['hourly'])]
    import numpy as np
    A = np.array([[x if x is not None else np.nan for x in h] for _, h in rows], dtype=float)
    fig, ax = plt.subplots(figsize=(10, 0.32 * len(rows) + 1.2))
    im = ax.imshow(A, aspect='auto', cmap='viridis')
    ax.set_yticks(range(len(rows))); ax.set_yticklabels([r[0] for r in rows], fontsize=8)
    ax.set_xticks(range(24)); ax.set_xticklabels([f'{h:02d}' for h in range(24)], fontsize=8); ax.set_xlabel('local hour')
    fig.colorbar(im, label='% of sweeps active'); fig.tight_layout()
    return fig


# ---------------------------------------------------------------- text
def build(S):
    D = Doc()
    site = S.get('site') or {}
    cov, lora, sun, fsk, P = S['coverage'], S.get('lora') or {}, S.get('sunfsk'), S.get('fsk'), S.get('power')
    title = f"902-928 MHz spectrum survey: {site.get('name') or 'unnamed site'}"
    D.h(1, title)
    by = [x for x in (site.get('location'), site.get('operator')) if x]
    D.p(' · '.join(by + [f"report generated {S['generated'].replace('T', ' ')}"]))

    # Summary
    D.h(2, 'Summary')
    classes = [c for c in lora.get('classes', []) if c['crc_ok']]
    if sun and sun['sfd']:
        v = ', '.join(f"{k} ({c})" for k, c in list(sun['vendors'].items())[:2])
        D.p(f"IEEE 802.15.4g SUN-FSK (50 kbps) traffic was found in {len({w for w, s in sun['per_window'].items() if s['sfd']})} of "
            f"{len(cov['windows'])} windows: {pl(sun['sfd'], 'frame start')}, {n(sun['ok'])} with a valid checksum, and "
            f"{pl(sun['distinct_eui64'], 'distinct 64-bit device address', 'distinct 64-bit device addresses')}."
            + (f" Device addresses by vendor (IEEE OUI): {v}." if v else ''))
    if classes:
        parts = [f"{c['label']} at {c['mhz']:.3f} MHz ({bwk(c['bw'])} / SF{c['sf']}, {pl(c['crc_ok'], 'valid frame')})" for c in classes[:5]]
        D.p(f"LoRa: {pl(lora['detections'], 'preamble')} detected, {pl(lora['crc_ok'], 'frame')} decoded with a valid CRC. Decoded classes: " + '; '.join(parts) + '.')
    elif lora:
        D.p(f"LoRa: {n(lora['detections'])} preambles detected, but no frame decoded with a valid CRC.")
    if fsk and fsk['families']:
        other = [f for f in fsk['families'] if not f['sun_fsk']]
        if other:
            D.p(f"{len(other)} other FSK famil{'y' if len(other) == 1 else 'ies'} with a measurable symbol rate: " +
                ', '.join(f"{f['name']} {f['baud'] / 1000:g} kbaud" for f in other) + '. None is identified by protocol.')
    if P:
        car = P.get('narrowband') or []
        D.p(f"Power survey ({P['hours']:g} h): some part of the band was active in {P['band_active_pct']:g}% of sweeps; wideband events "
            f"(about 1 MHz or more at once) in {P['wideband']['pct']:g}%." +
            (f" Narrowband carrier{'s' if len(car) > 1 else ''}: " + ', '.join(f"{c['mhz']:.3f} MHz ({c['first']} to {c['last'][-5:]})" for c in car[:3]) + '.' if car else ''))

    D.h(2, 'Band map')
    D.img('band_map.png', band_map(S), 'Top: 802.15.4g frames with a valid checksum per channel. Lanes: where each other signal class was found. Frequencies corrected for the receiver offset.')

    # Inventory
    D.h(2, 'Signal inventory')
    rows = []
    if sun and sun['sfd']:
        g = sun.get('mac_grid')
        rows.append(['802.15.4g SUN-FSK', '2-FSK 50 kbps', g['text'] if g else '', f"{n(sun['ok'])} valid of {n(sun['sfd'])}", ', '.join(list(sun['vendors'])[:2]) or 'decoded frames, vendor unknown'])
        for sf_ in sun['short_formats']:
            rows.append([f"Short {sf_['len']}-byte frames ({sf_['start']})", '2-FSK 50 kbps', sf_['grid']['text'] if sf_['grid'] else '',
                         f"{n(sf_['frames'])} valid, {sf_['mhz_min']}-{sf_['mhz_max']} MHz", 'not an 802.15.4 MAC header'])
    for c in lora.get('classes', []):
        if c['header_ok'] >= 2:
            rows.append([c['label'], f"LoRa {bwk(c['bw'])} / SF{c['sf']}", f"{c['mhz']:.3f} MHz", f"{c['header_ok']} headers, {c['crc_ok']} CRC-valid",
                         'decoded' if c['crc_ok'] else 'header only, unconfirmed'])
    for u in lora.get('undecoded', []):
        if u['detections'] >= 3:
            if u['same_mode_as'] or u['same_slope_as']:
                continue  # weak frames of a decoded class, or that class seen a second way: listed in the LoRa section
            rows.append([f"Undecoded LoRa", f"LoRa {bwk(u['bw'])} / SF{u['sf']}", f"{u['mhz']:.3f} MHz", f"{u['detections']} detections", 'unknown'])
    for f in (fsk or {}).get('families', []):
        if not f['sun_fsk']:
            rows.append([f"FSK family {f['name']}", f"2-FSK {f['baud'] / 1000:g} kbaud, dev ~{f['dev_hz'] / 1000:g} kHz, {f['dur_ms']} ms",
                         f['grid'] or '', f"{f['bursts']} measured bursts", 'unknown'])
    for c in (P or {}).get('narrowband', []):
        rows.append(['Narrowband carrier', 'unmodulated or very narrow', f"{c['mhz']:.4f} MHz", f"{c['first']} to {c['last']}", 'unknown'])
    D.table(['Signal', 'Modulation', 'Channels', 'Activity', 'Identity'], rows)

    # Coverage
    D.h(2, 'Coverage and method')
    ppm = S['ppm']
    D.table(['Dataset', 'When (local)', 'What'], [r for r in [
        ['Power survey (rtl_power)', f"{P['start'].replace('T', ' ')} to {P['end'].replace('T', ' ')}" if P else 'not run',
         f"{P['band_mhz'][0]}-{P['band_mhz'][1]} MHz in {P['bin_khz']} kHz bins, one sweep every {P['sweep_interval_s']} s ({n(P['sweeps'])} sweeps)" if P else ''],
        ['Raw-IQ scan', f"{cov['start']} to {cov['end']}" if cov['start'] else 'not run',
         f"{len(cov['windows'])} windows of 2.4 MHz, {cov['dwell_s']:g} s each, {cov['passes']} passes, {cov['dwells']} dwells ({cov['processed']} fully processed)"]]])
    D.p(f"Frequency correction: the receiver reads {ppm['ppm']:+g} ppm (source: {ppm['source']}). All frequencies here are corrected.")
    D.p('Each dwell went through a blind LoRa search (62.5-500 kHz x SF7-12) and decoder, an 802.15.4g SUN-FSK frame decoder, an FSK burst '
        'survey (first minute of each dwell) and an ADC level check. Payloads are never stored: only header fields, lengths and short hashes.')
    if cov['short_captures']:
        D.p(f"{cov['short_captures']} capture(s) came back short (USB sample loss or a stopped dongle) and were discarded.")

    # LoRa
    D.h(2, 'LoRa')
    if lora:
        D.p(f"{n(lora['detections'])} LoRa preambles in {cov['dwells']} dwells; {n(lora['header_ok'])} gave a valid header and {n(lora['crc_ok'])} a valid CRC. "
            f"Detections by mode: " + ', '.join(f"{k} {v}" for k, v in list(lora['detections_by_mode'].items())[:6]) + '.')
        D.table(['Class', 'MHz (corrected)', 'BW / SF', 'Sync', 'Headers OK', 'CRC OK', 'SNR (dB)', 'First / last'],
                [[c['label'], f"{c['mhz']:.4f}", f"{bwk(c['bw'])} / SF{c['sf']}", ', '.join(str(k) for k in c['syncs']), c['header_ok'], c['crc_ok'],
                  f"{c['snr_min']:.0f} to {c['snr_max']:.0f}", f"{c['first'][5:]} / {c['last'][5:]}"] for c in lora['classes'] if c['crc_ok']])
        hdr_only = [c for c in lora['classes'] if not c['crc_ok']]
        if hdr_only:
            D.p('Valid header but never a valid CRC. The LoRa header checksum is only 5 bits, so about 1 noise detection in 32 passes it; '
                'treat a group with one or two headers as unconfirmed. The name is only the channel raster it falls on.')
            D.table(['Raster match', 'MHz (corrected)', 'BW / SF', 'Sync', 'Headers OK', 'SNR (dB)', 'First / last'],
                    [[c['label'], f"{c['mhz']:.4f}", f"{bwk(c['bw'])} / SF{c['sf']}", ', '.join(str(k) for k in c['syncs']), c['header_ok'],
                      f"{c['snr_min']:.0f} to {c['snr_max']:.0f}", f"{c['first'][5:]} / {c['last'][5:]}"] for c in hdr_only])
        for c in lora['classes']:
            if not c['crc_ok']:
                continue
            if c['label'].startswith('Meshtastic'):
                hops = ', '.join(f"{k} hops: {v}" for k, v in c['hops'].items())
                D.p(f"{c['label']} ({c['mhz']:.3f} MHz, slot {c['slot']}): {pl(c['crc_ok'], 'valid frame')} = {pl(c['packets'], 'distinct packet')} from {pl(c['nodes'], 'node ID')}; "
                    f"{pl(c['broadcast'], 'broadcast')}, {c['want_ack']} asked for an acknowledgement. Channel hashes: "
                    + ', '.join(f"{k} ({v})" for k, v in c['channel_hashes'].items()) + f". Hops taken: {hops or 'unknown'}. Payloads were not decrypted or stored.")
            elif c['label'] == 'MeshCore':
                preset = 'the published US preset' if c.get('is_us_preset') else 'not the published US preset (910.525 MHz, 62.5 kHz, SF7)'
                D.p(f"MeshCore at {c['mhz']:.3f} MHz, {bwk(c['bw'])} / SF{c['sf']} ({preset}): {c['valid_structure']} of {c['crc_ok']} CRC-valid frames have valid MeshCore structure. "
                    f"Routes: {', '.join(f'{k} {v}' for k, v in c['routes'].items())}. Types: {', '.join(f'{k} {v}' for k, v in c['types'].items())}. "
                    f"Hops {c['hops_min']}-{c['hops_max']} (median {c['hops_median']}) through {c['relay_hashes']} distinct relay hashes; "
                    f"{c['adverts']} adverts. Payloads are encrypted and were not stored.")
            elif c['label'] == 'LoRaWAN Class B beacon':
                hop = {True: 'The beacons changed channel, as the US plan specifies.',
                       False: 'Consecutive beacons stayed on one channel, so this gateway does not hop beacon channels as the US plan (RP002) specifies.',
                       None: ''}[c['hopping']]
                real = [(a, b) for a, b in c['gw_location'] or [] if a and b]
                loc = ('The gateway field carries coordinates (rounded): ' + ', '.join(f"{a}, {b}" for a, b in real) + '.' if real else
                       'The gateway field carries a placeholder (' + ', '.join(f"{a}, {b}" for a, b in c['gw_location']) + '), not a location.' if c['gw_location'] else
                       'The gateway field carries no coordinates.')
                off = f" Beacon GPS time matched the receive time within {max(abs(x) for x in c['time_offset_s']):.1f} s." if c['time_offset_s'] else ''
                D.p(f"LoRaWAN Class B beacons: {pl(c['crc_ok'], 'beacon')} decoded with both beacon CRCs valid, at "
                    + ', '.join(f'{k} MHz ({v})' for k, v in c['channels'].items()) + f".{off} {hop} {loc} This is direct evidence of a LoRaWAN gateway in range."
                    .replace('  ', ' '))
            elif c['label'].startswith('LoRaWAN'):
                D.p(f"{c['label']} ({bwk(c['bw'])} / SF{c['sf']}): {pl(c['crc_ok'], 'valid frame')}, " + ', '.join(f'{k} {v}' for k, v in c['mtypes'].items()) +
                    f"; {c['dev_addrs']} distinct device addresses, {c['joins']} join requests. Channels: " + ', '.join(f'{x:g}' for x in c['channels'][:12]) + ' MHz.')
        if lora['undecoded']:
            D.h(3, 'Undecoded LoRa activity')
            D.p('Preambles that never gave a valid header. A mode with the same chirp slope (bandwidth squared over 2^SF) as a decoded class nearby is usually that class seen a second way.')
            D.table(['MHz', 'BW / SF', 'Detections', 'Visits', 'Median strength (dB)', 'First / last', 'Note'],
                    [[f"{u['mhz']:.3f}", f"{bwk(u['bw'])} / SF{u['sf']}", u['detections'], u['visits'], u['ratio_db_median'], f"{u['first'][5:]} / {u['last'][5:]}",
                      f"same mode as {u['same_mode_as']} (weak or collided frames)" if u['same_mode_as'] else
                      f"same slope as {u['same_slope_as']}" if u['same_slope_as'] else ''] for u in lora['undecoded'][:15]])
        if not any(c['label'] == 'LoRaWAN uplink' for c in lora['classes']):
            D.p('No LoRaWAN uplink (sync word 0x34) was decoded.')

    # 802.15.4g
    D.h(2, '802.15.4g SUN-FSK (50 kbps)')
    if sun and sun['sfd']:
        g = sun['mac_grid']
        D.p(f"{pl(sun['sfd'], 'frame start')} (SFD 0x904E), {n(sun['ok'])} ({sun['ok_pct']}%) with a valid frame check. "
            f"{pl(sun['distinct_eui64'], 'distinct 64-bit device address', 'distinct 64-bit device addresses')} in valid frames"
            + ('; vendors by IEEE OUI: ' + ', '.join(f"{k} {v}" for k, v in sun['vendors'].items()) + '.' if sun['vendors'] else '.')
            + (f" The 802.15.4 MAC frames use the grid {g['text']} ({100 * g['share']:.0f}% of their frame starts fall on it)." if g else ''))
        if sun['frame_types']:
            D.table(['Type', 'Frame version', 'Valid frames', 'Length (bytes)', 'Secured', 'Channels', 'MHz'],
                    [[t['type'], t['frame_ver'], t['frames'], f"{t['len_min']}-{t['len_max']}", t['secured'], t['channels'], f"{t['mhz_min']}-{t['mhz_max']}"] for t in sun['frame_types']])
        for s_ in sun['short_formats']:
            D.p(f"Short format: {n(s_['frames'])} valid {s_['len']}-byte frames starting {s_['start']} ({s_['distinct']} distinct), on {s_['channels']} channels "
                f"{s_['mhz_min']}-{s_['mhz_max']} MHz{' (grid ' + s_['grid']['text'] + ')' if s_['grid'] else ''}. They are not a valid 802.15.4 MAC header, so the format is proprietary or unknown.")
        if sun['pans']:
            D.table(['PAN ID', 'Data/command frames', 'Source addresses'], [[p_['pan'], p_['frames'], p_['sources']] for p_ in sun['pans'][:8]])
        if sun['top_source']:
            t = sun['top_source']
            D.p(f"Busiest source: {t['src']}, {t['frames']} frames ({100 * t['share']:.0f}%) on {t['channels']} channels in {t['windows']} windows, PAN {', '.join(t['pans'])}; probably a nearby router or collector.")
        if sun['ie_frames']:
            D.p(f"Frames carrying information elements: {n(sun['ie_frames'])}, of which {sun['ie_ok']} pass the frame check; {pct(sun['ie_struct_ok'], sun['ie_frames'])} have IEs that end exactly at the security tag "
                f"(random bytes do so about 2.5% of the time). Frames without IEs pass {pct(sun['non_ie_ok'], sun['non_ie_frames'])} of the time.")
        if sun['netnames']:
            D.p('Wi-SUN network names advertised: ' + ', '.join(sun['netnames']) + '.')
    else:
        D.p('No 802.15.4g SUN-FSK frames were found.')

    # Other FSK
    D.h(2, 'Other FSK families')
    if fsk and fsk['families']:
        D.p(f"{n(fsk['bursts'])} narrow non-LoRa bursts in the sampled minutes ({n(fsk['lora_excluded'])} LoRa bursts left out); {n(fsk['measurable'])} had a clean symbol-rate estimate and {n(fsk['classified'])} fall in a family "
            f"(at least {max(10, int(0.02 * fsk['measurable']))} bursts at one rate). Counts show where a family appears, not how much it transmits.")
        D.table(['Family', 'Symbol rate', 'Deviation', 'Frame (ms)', 'Bursts', 'Grid', 'MHz seen', 'Preamble bits', 'Sync after preamble', 'Note'],
                [[f['name'], f"{f['baud'] / 1000:g} kbaud", f"{f['dev_hz'] / 1000:g} kHz", f"{f['dur_ms']} ({f['dur_ms_range'][0]}-{f['dur_ms_range'][1]})", f['bursts'],
                  f['grid'] or 'none found', f"{f['mhz_min']}-{f['mhz_max']}", f['preamble_bits'], f['sync16'] or '', '802.15.4g SUN-FSK rate' if f['sun_fsk'] else ''] for f in fsk['families']])
    else:
        D.p('No FSK family had enough measurable bursts.')

    # Power
    if P:
        D.h(2, 'Power survey')
        D.p(f"{P['hours']:g} h, {n(P['sweeps'])} sweeps. A bin is active when it is more than {P['threshold_db']:g} dB above its own median over the surrounding hour. "
            f"Noise floor median {P['noise_floor_db']} dB (uncalibrated). Occupancy is the share of sweeps with activity, not airtime.")
        top = sorted(P['channels'], key=lambda c: -c['occupancy_pct'])[:10]
        D.table(['Channel (MHz, uncorrected)', 'Occupancy %', 'Peak dB', 'Floor dB'], [[c['mhz'], c['occupancy_pct'], c['peak_db'], c['floor_db']] for c in top])
        D.table(['Channel-plan group (energy only, not identification)', 'Occupancy %', 'Busiest channel'],
                [[g['name'], g['occupancy_pct'], f"{g['busiest_mhz']} MHz ({g['busiest_pct']}%)"] for g in P['groups']])
        D.img('power_hourly.png', power_hourly(P), 'Power survey: share of sweeps with activity, by channel-plan group and local hour.')
        if P['wideband']['pct']:
            D.p(f"Wideband events (40 or more adjacent active bins in one sweep) in {P['wideband']['pct']}% of sweeps, typically starting near {P['wideband']['low_edge_mhz_median']} MHz.")

    # Time of day
    fig = time_of_day(S)
    if fig:
        D.h(2, 'Time of day')
        D.img('time_of_day.png', fig, 'Activity at each visit through the IQ scan. Each window is visited once per pass, so one point or bar is one visit.')

    if S.get('receiver'):
        r = S['receiver']
        D.h(2, 'Receiver health')
        D.p(f"Noise median {r['rms_median']} ADC counts RMS. Clipping in {r['files_clipping']} of {cov['dwells']} dwells, {r['files_over_100ppm']} above 100 ppm of samples, worst {r['worst_clip_ppm']} ppm. "
            'Heavy clipping (well above 1000 ppm) means a strong nearby transmitter: lower the gain.')

    D.h(2, 'Caveats')
    D.bullets([
        'One site, one antenna, one period: traffic changes by day of week and season.',
        'Each window is heard for a few minutes per pass, so rare or slow transmitters (beacons, adverts, daily reports) can be missed.',
        'Power-survey occupancy is not airtime, and it cannot tell protocols apart; channel-plan groups only say where energy was.',
        'Weak signals are under-counted: LoRa below about -15 dB SNR and FSK bursts under about 25 dB rarely decode or classify.',
        'FSK families are measured from the first minute of each dwell only, and are unidentified unless a decoder confirms them.',
        'An RTL-SDR has an 8-bit ADC and a DC spike at each window centre; the decoders mask the centre bins.',
    ])
    D.h(2, 'Privacy')
    D.p('No payloads were stored or decrypted. The data keeps header fields (addresses, hop counts, frame types), lengths and 8-character hashes for duplicate detection. '
        'Device addresses are pseudonymous but persistent: share the data files with care.')
    return D, title


def main():
    run = sys.argv[1]
    S = json.load(open(os.path.join(run, 'summary.json')))
    D, title = build(S)
    out = os.path.join(run, 'report')
    D.write(out, title)
    print(f"report: {os.path.join(out, 'report.html')}")


if __name__ == '__main__':
    main()
