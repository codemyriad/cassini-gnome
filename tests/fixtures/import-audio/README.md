# L-25 import fixtures

One second of a 440 Hz sine tone, mono, 48 kHz, generated with GStreamer (no
third-party audio; CC0-1.0):

    gst-launch-1.0 audiotestsrc wave=sine freq=440 samplesperbuffer=4800 num-buffers=10 ! \
      audio/x-raw,rate=48000,channels=1 ! audioconvert ! lamemp3enc target=bitrate bitrate=32 ! \
      id3v2mux ! filesink location=tone.mp3
    gst-launch-1.0 audiotestsrc wave=sine freq=440 samplesperbuffer=4800 num-buffers=10 ! \
      audio/x-raw,rate=48000,channels=1 ! audioconvert ! avenc_aac bitrate=32000 ! \
      mp4mux ! filesink location=tone.m4a

WAV and the Ogg probe vectors are built in the tests with the standard library.
