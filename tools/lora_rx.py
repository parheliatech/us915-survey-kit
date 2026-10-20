"""gr-lora_sdr receive chain that accepts any sync word and returns structured frame records.

Needs the patched gr-lora_sdr (gr-lora_sdr-sdrplan.patch) and `. ./env.sh`.
Each detected frame yields a dict: sf, bw, sync (byte, rebuilt from the two net-ID symbols),
snr (dB, preamble estimate), cfo_bins, in_offset (input sample index), header_ok, cr, pay_len,
has_crc, crc_ok (None when the frame carries no payload CRC), payload (bytes, or None).
"""
import pmt
from gnuradio import gr, blocks
from gnuradio import lora_sdr


def _d(v):
    out = {}
    items = pmt.dict_items(v)
    for i in range(pmt.length(items)):
        kv = pmt.nth(i, items)
        out[pmt.symbol_to_string(pmt.car(kv))] = pmt.to_python(pmt.cdr(kv))
    return out


class RxChain(gr.hier_block2):
    """Complex input at bw*os sample rate, centred on the channel. Any sync word.
    center_freq (Hz) must be the real RF frequency: frame_sync derives clock drift from CFO / center_freq."""

    def __init__(self, sf, bw, os=4, center_freq=915e6, ldro_mode=2, soft=True, implicit=None):
        """implicit: None for explicit-header frames, or (cr, pay_len, has_crc) for implicit-header frames
        (e.g. LoRaWAN Class B beacons: (1, 23, False))."""
        gr.hier_block2.__init__(self, "rx_chain", gr.io_signature(1, 1, gr.sizeof_gr_complex),
                                gr.io_signature(0, 0, 0))
        self.sf, self.bw = sf, bw
        # frame_sync needs several symbols of input at once (SF11/12 exceed the default 8192-item buffer)
        self.buf = blocks.copy(gr.sizeof_gr_complex)
        self.buf.set_min_output_buffer(0, max(8192, 2 ** sf * os * 4))  # per-port form: the 1-arg form does not stick in GR 3.10.12
        self.sync = lora_sdr.frame_sync(int(center_freq), bw, sf, implicit is not None, [0], os, 8)
        self.demod = lora_sdr.fft_demod(soft, True)
        self.gray = lora_sdr.gray_mapping(soft)
        self.deint = lora_sdr.deinterleaver(soft)
        self.ham = lora_sdr.hamming_dec(soft)
        if implicit is None:
            self.hdr = lora_sdr.header_decoder(False, 1, 255, True, ldro_mode, False)
        else:
            cr, pay_len, has_crc = implicit
            self.hdr = lora_sdr.header_decoder(True, cr, pay_len, has_crc, ldro_mode, False)
        self.dewhite = lora_sdr.dewhitening()
        self.crc = lora_sdr.crc_verif(0, False)
        self.hdr_msgs = blocks.message_debug()
        self.out = blocks.vector_sink_b()
        self.connect(self, self.buf, self.sync, self.demod, self.gray, self.deint, self.ham, self.hdr,
                     self.dewhite, self.crc, self.out)
        self.msg_connect((self.hdr, 'frame_info'), (self.sync, 'frame_info'))
        self.msg_connect((self.hdr, 'frame_info'), (self.hdr_msgs, 'store'))

    def frames(self):
        """Merge header messages (all detections) with CRC-checked payloads, keyed by in_offset."""
        recs = {}
        for i in range(self.hdr_msgs.num_messages()):
            m = _d(self.hdr_msgs.get_message(i))
            if 'in_offset' not in m:
                continue  # frame_sync's own frame_info echoes are not headers
            n1, n2 = m.get('netid1', 0), m.get('netid2', 0)
            recs[m['in_offset']] = dict(
                sf=self.sf, bw=self.bw,
                sync=((round(n1 / 8) & 0xF) << 4) | (round(n2 / 8) & 0xF),
                netid=(n1, n2), snr=round(float(m.get('snr', 0)), 2),
                cfo_bins=round(m.get('cfo_int', 0) + m.get('cfo_frac', 0), 3),
                in_offset=m['in_offset'], header_ok=m['err'] == 0,
                cr=m['cr'], pay_len=m['pay_len'], has_crc=bool(m['crc']),
                crc_ok=None, payload=None)
        data = bytes(self.out.data())
        for t in self.out.tags():
            if pmt.symbol_to_string(t.key) != 'frame_info':
                continue
            m = _d(t.value)
            r = recs.get(m.get('in_offset'))
            if r is None:
                continue
            r['payload'] = data[t.offset:t.offset + m['pay_len']]
            if r['has_crc']:
                r['crc_ok'] = bool(m.get('crc_valid', False))
        return sorted(recs.values(), key=lambda r: r['in_offset'])
