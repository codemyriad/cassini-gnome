#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Chromium owns stdin/stdout. All protocol bytes go directly to the sandbox.
set -eu
set -f
browser_name=
browser_instance=
pid=$PPID
remaining=5
while [ "$pid" -gt 1 ] && [ "$remaining" -gt 0 ]; do
    executable=$(readlink "/proc/$pid/exe" 2>/dev/null || :)
    case ${executable##*/} in
        chrome|google-chrome|google-chrome-stable) browser_name=Chrome ;;
        chromium|chromium-browser) browser_name=Chromium ;;
        msedge|microsoft-edge|microsoft-edge-stable) browser_name=Edge ;;
        brave|brave-browser) browser_name=Brave ;;
    esac
    if [ -n "$browser_name" ]; then
        if read -r process < "/proc/$pid/stat"; then
            process=${process##*) }
            set -- $process
            if [ "$#" -ge 20 ]; then
                shift 19
                browser_instance=$pid:$1
            fi
        fi
        break
    fi
    parent=0
    if [ -r "/proc/$pid/status" ]; then
        while read -r key value rest; do
            case $key in PPid:) parent=$value; break ;; esac
        done < "/proc/$pid/status"
    fi
    pid=$parent
    remaining=$((remaining - 1))
done
exec @FLATPAK@ run --arch=@ARCH@ --branch=@BRANCH@ --command=@COMMAND@ \
    "--env=CASSINI_HOST_BROWSER_NAME=$browser_name" \
    "--env=CASSINI_HOST_BROWSER_INSTANCE=$browser_instance" @APPLICATION@
