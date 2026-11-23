FROM python:3.13-alpine
RUN adduser -D -u 10001 bridge && mkdir /data && chown bridge /data
WORKDIR /app
COPY bridge.py .
USER bridge
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
CMD ["python", "-u", "bridge.py"]
