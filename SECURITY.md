# Security

Cassini opens files that other people send you, so bugs in the format reader
matter. The reader bounds header size, chunk counts, decompressed size and
JSON depth, as the format specification requires.

Please report suspected vulnerabilities privately through
[GitHub security advisories](https://github.com/codemyriad/cassini-gnome/security/advisories/new)
rather than in a public issue.
