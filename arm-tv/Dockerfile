FROM rednoah/filebot:watcher

COPY --chmod=755 entrypoint.sh /usr/local/bin/arm-tv

ENTRYPOINT ["/opt/bin/run-as-user", "/opt/bin/run", "/usr/local/bin/arm-tv"]
