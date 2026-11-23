FROM python:3.13-alpine
# 99:100 is nobody:users on Unraid, so bind-mounted appdata is writable there
# without extra flags. Any other host just sees an unprivileged user.
RUN mkdir /data && chown 99:100 /data
WORKDIR /app
COPY bridge.py .
USER 99:100
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
CMD ["python", "-u", "bridge.py"]
