# SPDX-License-Identifier: GPL-3.0-or-later
"""G12: the Ogg Opus structure check every Library v2 output passes before it is published.

From REVIEWS/gocassini-lessons.md G12 (the validator gocassini converged on): the
first page is BOS and only it is; one logical stream (never chained or
multiplexed); contiguous page sequence numbers from 0; valid CRCs; OpusHead is
the first packet and OpusTags the second; the EOS page completes a packet and
nothing follows it; the final granule is >= pre-skip; and the playable length
is min(final granule, sum of packet samples) - pre-skip. Added here: continuation
flags agree with the lacing, granules never decrease, and with strict=True the
RFC 7845 header layout (OpusHead alone on page 0, OpusTags starting on page 1
and finishing its page) that our writers produce. strict=False accepts inputs
whose tags share a page with audio, which §6 rewrites must still read.
"""

import struct
from dataclasses import dataclass

from ..portable import ogg_crc, packet_samples


class OggError(ValueError):
    pass


@dataclass(frozen=True)
class Facts:
    serial: int
    channels: int
    pre_skip: int
    final_granule: int
    decoded: int  # sum of audio packet durations, 48 kHz samples
    playable: int  # min(final granule, decoded) - pre-skip
    pages: int
    audio_packets: int
    audio_granules: tuple  # granule of every page that completes an audio packet, in order


def validate(stream, *, strict=True):
    stream.seek(0)
    serial = channels = pre_skip = final = None
    partial = bytearray()
    index = sequence = decoded = audio_packets = 0
    granules = []
    last_granule = 0
    eos = False
    while True:
        header = stream.read(27)
        if not header:
            break
        if eos:
            raise OggError("data after the end-of-stream page")
        if len(header) < 27 or header[:4] != b"OggS" or header[4] != 0:
            raise OggError("bad Ogg page header")
        flags, granule, page_serial, page_sequence, crc = struct.unpack_from("<BQIII", header, 5)
        lacing = stream.read(header[26])
        payload = stream.read(sum(lacing))
        if len(lacing) != header[26] or len(payload) != sum(lacing):
            raise OggError("truncated Ogg page")
        if ogg_crc(header[:22] + bytes(4) + header[26:] + lacing + payload) != crc:
            raise OggError(f"CRC mismatch on page {page_sequence}")
        if flags & ~0x07:
            raise OggError("unknown page flags")
        if bool(flags & 0x02) != (sequence == 0):
            raise OggError("beginning-of-stream must mark the first page only")
        if serial is None:
            serial = page_serial
        elif page_serial != serial:
            raise OggError("chained or multiplexed Ogg stream")
        if page_sequence != sequence:
            raise OggError(f"page sequence gap at page {page_sequence}")
        if bool(flags & 0x01) != bool(partial):
            raise OggError(f"continuation flag disagrees with the lacing on page {page_sequence}")
        starts_at = index
        completed_audio = False
        offset = 0
        for size in lacing:
            partial += payload[offset : offset + size]
            offset += size
            if size == 255:
                continue
            packet = bytes(partial)
            partial.clear()
            if index == 0:
                if len(packet) < 19 or packet[:8] != b"OpusHead" or packet[8] >> 4 or packet[9] not in (1, 2):
                    raise OggError("malformed OpusHead")
                if packet[18] != 0:
                    raise OggError("only channel mapping family 0 is accepted")
                channels, pre_skip = packet[9], struct.unpack_from("<H", packet, 10)[0]
            elif index == 1:
                if packet[:8] != b"OpusTags":
                    raise OggError("second packet is not OpusTags")
            else:
                try:
                    decoded += packet_samples(packet)
                except ValueError as error:
                    raise OggError(str(error)) from None
                audio_packets += 1
                completed_audio = True
            index += 1
        if strict:
            if sequence == 0 and (index != 1 or partial):
                raise OggError("OpusHead must be alone on the first page")
            if sequence == 1 and starts_at != 1:
                raise OggError("OpusTags must begin on the second page")
            if starts_at <= 1 < index and (index > 2 or partial):
                raise OggError("OpusTags shares its last page with audio")
        if completed_audio:
            if granule == 0xFFFFFFFFFFFFFFFF or granule < last_granule:
                raise OggError(f"granule position goes backwards on page {page_sequence}")
            last_granule = granule
            granules.append(granule)
        if flags & 0x04:
            if partial or not completed_audio:
                raise OggError("the end-of-stream page does not complete an audio packet")
            eos = True
            final = granule
        sequence += 1
    if not eos:
        raise OggError("missing end-of-stream page")
    if index < 3:
        raise OggError("no audio packets")
    if final > 0x7FFFFFFFFFFFFFFF or final < pre_skip:
        raise OggError("final granule is below pre-skip")
    if decoded < pre_skip:
        raise OggError("fewer decoded samples than pre-skip")
    return Facts(
        serial=serial,
        channels=channels,
        pre_skip=pre_skip,
        final_granule=final,
        decoded=decoded,
        playable=min(final, decoded) - pre_skip,
        pages=sequence,
        audio_packets=audio_packets,
        audio_granules=tuple(granules),
    )


def validate_path(path, *, strict=True):
    with open(path, "rb") as stream:
        return validate(stream, strict=strict)
