ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install Python3 and dependencies via APK
RUN apk add --no-cache \
    python3 \
    py3-requests \
    ca-certificates \
    tzdata

# 设置时区为东八区（北京时间）
ENV TZ=Asia/Shanghai
RUN cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    echo "Asia/Shanghai" > /etc/timezone

# Copy application modules
COPY client.py /
COPY retention.py /
COPY sync.py /
COPY notifier.py /
COPY main.py /

# Start the add-on
CMD [ "python3", "/main.py" ]