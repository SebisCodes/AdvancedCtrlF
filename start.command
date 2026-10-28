#!/bin/bash
# =====================================================================
#  Advanced Ctrl+F - launcher for macOS
#
#  Double-click this file in Finder. It opens Terminal and runs start.sh,
#  which sets up a local Python on the first start and launches the app.
#
#  First start only: macOS may say the file "cannot be opened because it is
#  from an unidentified developer". Then right-click (or Ctrl-click) this
#  file -> Open -> Open. This is needed once per downloaded copy.
# =====================================================================
cd "$(dirname "$0")" || exit 1
bash ./start.sh ${@+"$@"}
status=$?
if [ $status -ne 0 ]; then
    echo
    echo "[Advanced Ctrl+F] Something went wrong - see the messages above and data/setup.log."
    [ -t 0 ] && read -r -p "Press Enter to close this window ..." _
fi
exit $status
