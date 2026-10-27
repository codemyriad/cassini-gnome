# Manual product runs: Teams, Zoom, then Meet

These are live account checks for Silvio. Start with [setup.md](setup.md), then follow the
numbered product procedure. Use only dedicated test calls and generated input. Real product
behavior is an observation to record, not something inferred from the topology fixtures.
Chromium is the current target (Chrome 116 minimum).

Use the exact popup buttons **Record separate tracks**, **Record the whole tab**, and **Stop and save**.
Each recording creates `~/Music/Cassini/Recording-<UUID>/`; copy the path shown in the popup.
Retain that folder. After Stop, attach session.json and the output of
`python3 conformance/summarize.py /absolute/Recording-UUID --tones 440,880` to a result
using TEMPLATE.md. Copy sanitized handwritten results into results/; generated CI model
results are only in artifacts/ and the Actions artifact, not results/.

Manual order: Teams two-person call; Zoom two-person call and explicit tab fallback; Meet
four-person call if accounts permit. Mark unavailable accounts/devices as blocked. Do not
report a device switch, live version or speaker association that was not actually observed.
