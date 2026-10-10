FROM golang:1.22.4-alpine3.20@sha256:ace6cc3fe58d0c7b12303c57afe6d6724851152df55e08057b43990b927ad5e8 AS build

ARG MINIO_VERSION=RELEASE.2024-06-13T22-53-53Z
ARG MINIO_COMMIT=20960b6a2ddb9594ee418035b3c7c7fe92ae6a12

RUN apk add --no-cache git
WORKDIR /src
RUN git init \
    && git remote add origin https://github.com/minio/minio.git \
    && git fetch --depth 1 origin "refs/tags/${MINIO_VERSION}" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${MINIO_COMMIT}"
RUN LDFLAGS="$(MINIO_RELEASE=RELEASE go run buildscripts/gen-ldflags.go)" \
    && CGO_ENABLED=0 go build -tags kqueue -trimpath -ldflags "${LDFLAGS}" -o /out/minio .

FROM alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc

RUN apk add --no-cache ca-certificates
COPY --from=build /out/minio /usr/local/bin/minio

EXPOSE 9000 9001
VOLUME ["/data"]
ENTRYPOINT ["/usr/local/bin/minio"]