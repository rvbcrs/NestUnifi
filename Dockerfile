# Build both patched servers from their pinned sources, so the image needs nothing from the host.
# This stage runs on the build machine's own processor. The scripts run their tests there and
# compile the final binaries for the processor the image is meant for.
FROM --platform=$BUILDPLATFORM golang:1.27-alpine AS build
ARG TARGETARCH
RUN apk add --no-cache git patch
COPY suite /src/suite
RUN GOARCH=$TARGETARCH sh /src/suite/build-bridge.sh /out/nest-bridge
RUN GOARCH=$TARGETARCH sh /src/suite/build-camera.sh /out/nest-camera
RUN wget -q -O /out/mediamtx-LICENSE https://raw.githubusercontent.com/bluenviron/mediamtx/v1.20.1/LICENSE

FROM bluenviron/mediamtx:1.20.1@sha256:1b029d11049be75630e9b73bb0d5f47b08a7db4eaee89a80bf8f53bc40e56414 AS media
FROM ghcr.io/aiaid/onvif-proxy@sha256:27e15a45dd566ac2243aa9b8595fd13d0c142ce11a5660011747456401fdcc08
USER root
RUN apk add --no-cache python3 iproute2 ca-certificates && mkdir -p /state/private && chmod 0700 /state/private && ln -s /config /state/private/bridge && ln -s /media-config /state/private/media
COPY --from=build /out/nest-bridge /out/nest-camera /usr/local/bin/
COPY --from=media /mediamtx /usr/local/bin/mediamtx
COPY admin/web.py admin/worker.py admin/mixer.py admin/google_api.py admin/index.html admin/app.css admin/app.js suite/runtime.py suite/lease.py /app/
COPY admin/assets /app/assets
COPY UPSTREAM-LICENSE /usr/share/licenses/nest-bridge/LICENSE
COPY suite/upstream/LICENSE /usr/share/licenses/onvif-proxy/LICENSE
COPY --from=build /out/mediamtx-LICENSE /usr/share/licenses/mediamtx/LICENSE
LABEL org.opencontainers.image.title="NestUnifi" \
      org.opencontainers.image.description="Google Nest cameras, doorbells and hubs as ONVIF cameras for UniFi Protect"
# The published version and where newer ones appear. A local build is "dev" and never looks for updates.
ARG NESTUNIFI_VERSION=dev
ARG NESTUNIFI_REPOSITORY=
ENV NESTUNIFI_VERSION=$NESTUNIFI_VERSION NESTUNIFI_REPOSITORY=$NESTUNIFI_REPOSITORY
WORKDIR /state
ENTRYPOINT ["python3", "-IB", "/app/runtime.py"]
CMD []
