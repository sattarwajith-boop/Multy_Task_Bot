tracker_list=$(curl -fsSL --connect-timeout 5 --max-time 10 \
  https://ngosang.github.io/trackerslist/trackers_all_http.txt 2>/dev/null \
  | awk '$0' | tr '\n\n' ',' || true)
aria2c --allow-overwrite=true --auto-file-renaming=true --bt-enable-lpd=true --bt-detach-seed-only=true \
       --bt-remove-unselected-file=true --bt-tracker="$tracker_list" --bt-max-peers=80 --enable-rpc=true \
       --rpc-listen-all=false --rpc-listen-port=6800 --rpc-max-request-size=1024M \
       --max-connection-per-server=8 --max-concurrent-downloads=3 --split=8 \
       --seed-ratio=0 --check-integrity=true --continue=true --daemon=true --disk-cache=40M --force-save=true \
       --min-split-size=10M --follow-torrent=mem --check-certificate=false --optimize-concurrent-downloads=true \
       --http-accept-gzip=true --max-file-not-found=0 --max-tries=10 --reuse-uri=true \
       --content-disposition-default-utf8=true --user-agent=Wget/1.12 --peer-agent=qBittorrent/4.5.2 --quiet=true \
       --summary-interval=0 --max-upload-limit=1K
