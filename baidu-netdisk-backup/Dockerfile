ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install Python3 and dependencies via APK
RUN apk add --no-cache \
    python3 \
    py3-requests \
    ca-certificates \
    tzdata

# Copy application modules
COPY client.py /
COPY retention.py /
COPY sync.py /
COPY main.py /

# Start the add-on
CMD [ "python3", "/main.py" ]