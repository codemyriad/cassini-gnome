# CLDR calendar data

Calendar names and full-date patterns are extracted from Unicode CLDR JSON 49.0.0, commit
`a4b67247b446fd51b475991017a2481911210c60` in https://github.com/unicode-org/cldr-json.
English uses en-GB; Portuguese (pt) supplies pt-BR; pt-PT supplies European
Portuguese. D50.3 explicitly chooses the comma-free English full-date pattern
`EEEE d MMMM y`; CLDR en-GB supplies `EEEE, d MMMM y`. All other patterns and
calendar names are unmodified.

Copyright © 2015-2024 Unicode, Inc.
Distributed under Unicode-3.0; see CLDR-LICENSE.txt.
Source hashes and the reduced data hash are in tests/i18n/CLDR-SOURCES.json.
No data is downloaded at build or run time.
