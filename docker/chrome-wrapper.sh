#!/usr/bin/env bash
# Chrome wrapper that enables Chrome DevTools Protocol (CDP) remote debugging.
# Mounted into the KasmVNC container at /usr/bin/google-chrome to replace the
# default launcher. CDP listens on port 9222 inside the container.
if ! pgrep chrome > /dev/null;then
  rm -f $HOME/.config/google-chrome/Singleton*
fi
sed -i 's/"exited_cleanly":false/"exited_cleanly":true/' ~/.config/google-chrome/Default/Preferences
sed -i 's/"exit_type":"Crashed"/"exit_type":"None"/' ~/.config/google-chrome/Default/Preferences
if [ -f /opt/VirtualGL/bin/vglrun ] && [ ! -z "${KASM_EGL_CARD}" ] && [ ! -z "${KASM_RENDERD}" ] && [ -O "${KASM_RENDERD}" ] && [ -O "${KASM_EGL_CARD}" ] ; then
    echo "Starting Chrome with GPU Acceleration on EGL device ${KASM_EGL_CARD}"
    vglrun -d "${KASM_EGL_CARD}" /opt/google/chrome/google-chrome --password-store=basic --no-sandbox --ignore-gpu-blocklist --user-data-dir --no-first-run --disable-search-engine-choice-screen --simulate-outdated-no-au='Tue, 31 Dec 2099 23:59:59 GMT' --remote-debugging-port=9222 --remote-debugging-address=0.0.0.0 --remote-allow-origins=* "$@"
else
    echo "Starting Chrome"
    /opt/google/chrome/google-chrome --password-store=basic --no-sandbox --ignore-gpu-blocklist --user-data-dir --no-first-run --disable-search-engine-choice-screen --simulate-outdated-no-au='Tue, 31 Dec 2099 23:59:59 GMT' --remote-debugging-port=9222 --remote-debugging-address=0.0.0.0 --remote-allow-origins=* "$@"
fi
