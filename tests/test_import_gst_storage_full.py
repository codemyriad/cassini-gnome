# SPDX-License-Identifier: GPL-3.0-or-later
"""D-1059 / BUG-CLASSES #15: a disk that fills during audio conversion is
storage-full, not "could not be imported".

GStreamer's filesink reports ENOSPC as ``ResourceError.NO_SPACE_LEFT`` on the
bus; decode_to_opus must keep that as ``OSError(ENOSPC)`` instead of folding
it into the ValueError used for damaged audio. The real-pipeline tests point
the Opus output at ``/dev/full`` (through a symlink, so the cleanup unlink
only removes the link), which fails every write with ENOSPC.
"""

import errno
import os
import zipfile

import pytest
from test_import_types import needs_encoder, write_wav

from cassini_talk import audio_import, batch_import, import_feedback, received
from cassini_talk.i18n import LANGUAGES, using_language
from cassini_talk.strings import text

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

Gst.init(None)
DEV_FULL = "/dev/full"
needs_dev_full = pytest.mark.skipif(not os.path.exists(DEV_FULL), reason="/dev/full unavailable")
RAW = ("No space left", "Errno", "filesink", "gst_", "resource")


def gst_error(code, message="No space left on the resource.", domain=None):
    return GLib.Error.new_literal(domain or Gst.resource_error_quark(), message, int(code))


def test_no_space_left_bus_error_is_enospc_oserror():
    raised = audio_import.bus_error(Gst, gst_error(Gst.ResourceError.NO_SPACE_LEFT), "gstelements_private.c(364)")
    assert isinstance(raised, OSError) and raised.errno == errno.ENOSPC
    assert import_feedback.storage_full(raised)
    assert import_feedback.failure_reason(raised) == "diskFull"


def test_write_error_with_quota_or_enospc_debug_is_storage_full():
    for code in (errno.EDQUOT, errno.ENOSPC):
        debug = f'Error while writing to file "/x.opus": {os.strerror(code)}'
        raised = audio_import.bus_error(Gst, gst_error(Gst.ResourceError.WRITE, "Could not write"), debug)
        assert isinstance(raised, OSError) and raised.errno == code


def test_other_bus_errors_stay_damaged_valueerrors():
    cases = [
        (gst_error(Gst.ResourceError.WRITE, "Could not write"), f"write: {os.strerror(errno.EACCES)}"),
        (gst_error(Gst.ResourceError.WRITE, "Could not write"), None),
        (gst_error(Gst.ResourceError.READ, "Could not read"), None),
        (gst_error(Gst.StreamError.DECODE, "Could not decode stream.", Gst.stream_error_quark()), None),
        # Same numeric code (14) in another domain is not storage-full.
        (gst_error(Gst.ResourceError.NO_SPACE_LEFT, "other", Gst.stream_error_quark()), None),
    ]
    for error, debug in cases:
        raised = audio_import.bus_error(Gst, error, debug)
        assert type(raised) is ValueError and not import_feedback.storage_full(raised)


def test_simulated_no_space_left_bus_message_from_decode_to_opus(tmp_path, monkeypatch):
    """The bus loop itself turns a NO_SPACE_LEFT message into ENOSPC and cleans up."""
    source = write_wav(tmp_path / "Weekly sync.wav")
    output = tmp_path / "out.opus"
    real_parse_launch = Gst.parse_launch

    def parse_launch(description):
        pipeline = real_parse_launch(description)
        sink = pipeline.get_by_name("output")
        message = Gst.Message.new_error(sink, gst_error(Gst.ResourceError.NO_SPACE_LEFT), "simulated")
        pipeline.get_bus().post(message)
        return pipeline

    monkeypatch.setattr(Gst, "parse_launch", parse_launch)
    if audio_import.missing_elements():
        pytest.skip("GStreamer Opus encode path unavailable")
    with pytest.raises(OSError) as caught:
        audio_import.decode_to_opus(source, output)
    assert caught.value.errno == errno.ENOSPC
    assert not output.exists()


def full_output(monkeypatch):
    """Route every decode_to_opus output to /dev/full via a symlink."""
    real = audio_import.decode_to_opus

    def decode(source, output, **kwargs):
        os.symlink(DEV_FULL, output)
        return real(source, output, **kwargs)

    monkeypatch.setattr(received, "decode_to_opus", decode)


@needs_encoder
@needs_dev_full
def test_real_filesink_write_to_full_disk_is_enospc(tmp_path):
    source = write_wav(tmp_path / "Weekly sync.wav")
    output = tmp_path / "out.opus"
    os.symlink(DEV_FULL, output)
    with pytest.raises(OSError) as caught:
        audio_import.decode_to_opus(source, output)
    assert caught.value.errno == errno.ENOSPC
    assert not os.path.lexists(output)
    assert os.path.exists(DEV_FULL)


@needs_encoder
@needs_dev_full
def test_single_file_conversion_on_full_disk_says_free_space(tmp_path, monkeypatch):
    full_output(monkeypatch)
    source = write_wav(tmp_path / "Weekly sync.wav")
    with pytest.raises(OSError) as caught:
        received.import_one(source, tmp_path / "Library")
    assert import_feedback.storage_full(caught.value)
    for lang in LANGUAGES:
        with using_language(lang):
            copy = import_feedback.single_failure(caught.value)
            assert copy == text("import.failed.diskFull")
        assert not any(raw in copy for raw in RAW)


@needs_encoder
@needs_dev_full
@pytest.mark.parametrize("lang", LANGUAGES)
def test_batch_and_zip_conversion_on_full_disk_say_free_space(tmp_path, monkeypatch, lang):
    full_output(monkeypatch)
    loose = write_wav(tmp_path / "Weekly sync.wav")
    member = write_wav(tmp_path / "member.wav")
    archive = tmp_path / "Notes.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.write(member, "Planning.wav")
    result = batch_import.import_files([loose, archive], tmp_path / "Library")
    assert result.failed == 2 and result.imported == 0
    assert sorted(result.errors) == [("Planning.wav", "diskFull"), ("Weekly sync.wav", "diskFull")]
    assert str(loose) in result.failed_inputs
    with using_language(lang):
        message = import_feedback.summary(result)
        assert text("import.failed.diskFull") in message
        assert text("import.zip.failed.damaged") not in message
    assert not any(raw in message for raw in RAW)
