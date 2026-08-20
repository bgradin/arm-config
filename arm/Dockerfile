FROM automaticrippingmachine/automatic-ripping-machine:latest

# Keep disc access and ARM ownership in the ARM container.  This image adds
# only the small capture/handoff client; analysis and library writes happen in
# the unprivileged tv service.
COPY tv /opt/arm/tv
COPY arm/bridge.py /opt/arm/bridge.py
COPY arm/patch.py /tmp/patch.py

RUN printf '%s\n' \
      'TV_DATA=/var/lib/tv' \
      'TV_INBOX=/home/arm/media/completed/tv' \
      'TV_LIBRARY=/home/arm/media/completed/tv/organized' \
      >> /etc/environment

RUN python3 /tmp/patch.py && rm /tmp/patch.py

ENV PYTHONPATH=/opt/arm
