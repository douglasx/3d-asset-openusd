# One image for build, validation and API. Pinned base and dependency versions
# so the same inputs give byte-identical outputs on a laptop, on-prem or cloud.
FROM python:3.13.1-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    PXR_PLUGINPATH_NAME=/app/schema/marine
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgl1 libglib2.0-0 libxrender1 libxext6 nodejs npm \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY marinekit ./marinekit
COPY generators ./generators
COPY api ./api
RUN pip install ".[dev]"
COPY build/package.json build/optimize.mjs ./build/
RUN cd build && npm install --no-audit --no-fund
COPY viewer/package.json ./viewer/
RUN cd viewer && npm install --no-audit --no-fund
COPY . .
# build assets, validate, build the viewer; fail the image if validation fails
RUN python -m marinekit.build && cd viewer && npm run build
EXPOSE 8000
CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000"]
