FROM python:3.13-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

# Dependencies resolve from PyPI; `serve` adds inbound OAuth/JWT auth and Sentry.
COPY pyproject.toml README.md LICENSE NOTICE /app/
COPY vgi_cloudflare /app/vgi_cloudflare
RUN pip install --no-cache-dir "/app[serve]" \
    && pip uninstall -y pip

ARG GIT_COMMIT=unknown
ENV VGI_CLOUDFLARE_GIT_COMMIT=${GIT_COMMIT}
ENV SENTRY_RELEASE=${GIT_COMMIT}

EXPOSE 8000
CMD ["sh", "-c", "vgi-cloudflare-http --host 0.0.0.0 --port ${PORT:-8000}"]
