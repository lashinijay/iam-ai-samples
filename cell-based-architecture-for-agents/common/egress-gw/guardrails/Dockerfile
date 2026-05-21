# Stage 1: build required ext_proc Python stubs.
# Sources: data-plane-api (envoy protos), udpa/xds annotations, validate.proto.

FROM python:3.12-slim AS proto-builder

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir grpcio-tools==1.68.1

WORKDIR /proto-src

# data-plane-api/udpa/xds are pinned to current main via shallow clone.
# protoc-gen-validate uses tagged release v1.1.0.
RUN git clone --depth=1 https://github.com/envoyproxy/data-plane-api.git && \
    git clone --depth=1 https://github.com/cncf/udpa.git && \
    git clone --depth=1 https://github.com/cncf/xds.git && \
    git clone --depth=1 --branch v1.1.0 \
      https://github.com/bufbuild/protoc-gen-validate.git

# Compile ext_proc + transitive deps. google/protobuf/* comes from grpcio-tools.
RUN mkdir /generated && \
    GRPC_PROTO=$(python -c "import grpc_tools, os; print(os.path.join(os.path.dirname(grpc_tools.__file__),'_proto'))") && \
    python -m grpc_tools.protoc \
      -I /proto-src/data-plane-api \
      -I /proto-src/udpa \
      -I /proto-src/xds \
      -I /proto-src/protoc-gen-validate \
      -I "$GRPC_PROTO" \
      --python_out=/generated \
      --grpc_python_out=/generated \
      envoy/service/ext_proc/v3/external_processor.proto \
      envoy/extensions/filters/http/ext_proc/v3/processing_mode.proto \
      envoy/config/core/v3/base.proto \
      envoy/config/core/v3/address.proto \
      envoy/config/core/v3/backoff.proto \
      envoy/config/core/v3/extension.proto \
      envoy/config/core/v3/http_uri.proto \
      envoy/config/core/v3/socket_option.proto \
      envoy/type/v3/http_status.proto \
      envoy/type/v3/percent.proto \
      envoy/type/v3/semantic_version.proto \
      envoy/annotations/deprecation.proto \
      udpa/annotations/migrate.proto \
      udpa/annotations/status.proto \
      udpa/annotations/versioning.proto \
      xds/annotations/v3/status.proto \
      xds/core/v3/context_params.proto \
      validate/validate.proto

# protoc omits __init__.py; add them so generated absolute imports resolve.
RUN find /generated -type d -exec touch {}/__init__.py \;

# Stage 2: runtime image.
FROM python:3.12-slim AS base

RUN groupadd -r appuser && useradd -r -g appuser -s /sbin/nologin appuser

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy generated stubs and app entrypoint.
COPY --from=proto-builder /generated/ /app/
COPY main.py .

USER appuser
EXPOSE 50052

ENTRYPOINT ["python", "main.py"]
