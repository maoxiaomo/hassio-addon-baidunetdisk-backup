ARG BUILD_FROM
FROM alpine:3.19

# 1. Change Alpine repo to Aliyun (HTTP) for reliability
RUN sed -i 's/dl-cdn.alpinelinux.org/mirrors.aliyun.com/g' /etc/apk/repositories \
    && sed -i 's/https/http/g' /etc/apk/repositories

# 2. Install Python3 and dependencies via APK
# python3: Alpine's native python
# py3-requests: pre-compiled requests library matching system python
# ca-certificates: for SSL verification
# tzdata: for timezone support
RUN apk add --no-cache \
    python3 \
    py3-requests \
    ca-certificates \
    tzdata

# Copy run script
COPY run.py /

# Run with python3 (which is now /usr/bin/python3 from apk)
CMD [ "python3", "/run.py" ]
