# Cassini browser recording protocol snapshot

This neutral directory is the interim shared contract. There is no remote upstream yet.
`SPEC.md`, schemas, raw JSON vectors, and the stdio runner are portable. `extension/` is
an exact reviewed snapshot; each app keeps its installed extension byte-identical to it.
Do not edit the app copy independently. `python3 browser-protocol/verify.py BrowserExtension`
checks both the snapshot SHA list and the installed copy. GNOME passes `browser-extension`.

Run vectors against either host:

```
python3 browser-protocol/conformance/run.py /absolute/path/to/native-host
```

The executable must use native-messaging stdio and return a session folder in `started`.
The runner deletes only session folders created by its own requests. On failure it retains
them and prints their paths. Vectors assert response type/code and file results, never error
wording. Schemas describe structure; lexical integer and byte limits also require vectors.

Browser scenarios stay in the app adapter directory for now (`BrowserExtension/tests` on
Mac and `tests/recording` on GNOME). They use the same extension snapshot. A complete merged
cross-platform browser driver and Talk/HPB/MLS/soak matrix remain a follow-up, not a claim of
coverage from these vectors. Hardware-native tests are separate platform release gates.
