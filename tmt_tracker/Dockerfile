ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-aiohttp py3-yaml

COPY app /app

# with-contenv passes the Supervisor environment (SUPERVISOR_TOKEN) through s6-overlay to the app.
CMD ["/usr/bin/with-contenv", "python3", "-u", "/app/main.py"]
