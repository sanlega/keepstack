# Official emulator image (recipe from the AWS guide); the architecture is picked automatically:
# x86_64 on Intel/AMD, aarch64 on Apple Silicon.
FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal
ARG TARGETARCH
RUN case "$TARGETARCH" in arm64) A=aarch64 ;; *) A=x86_64 ;; esac \
    && curl -fsSL -o /tmp/aws-emulator.rpm "https://downloads.aws.com/aws-emulator/packages/aws-emulator/LTS/latest/aws-emulator-$A.rpm" \
    && rpm -i /tmp/aws-emulator.rpm && rm /tmp/aws-emulator.rpm \
    && mkdir -p /home/emulator && chown 1000 /home/emulator
USER 1000
ENV HOME=/home/emulator
EXPOSE 9297
ENTRYPOINT ["aws-emulator", "-b", "0.0.0.0"]
