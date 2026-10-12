# Arch test environment for Usual Install.
#
#   docker build -t usual-install-test -f test/Dockerfile .
#   docker run --rm usual-install-test            # tiers 1-2
#   docker run --rm -it --entrypoint bash usual-install-test   # poke around
#
# On Apple Silicon add --platform linux/amd64 to both commands: Arch publishes
# x86_64 images only. It runs under emulation, slowly but correctly.
#
# Flatpak cannot meaningfully install inside an unprivileged container, so the
# installer runs with USUAL_DRY_RUN=1 here. A real flatpak install and a look at
# the actual GTK window need an Arch VM -- see README.
FROM archlinux:latest

# --disable-sandbox: pacman's seccomp sandbox cannot initialise under QEMU user
# emulation, which is how this x86_64 image runs on an Apple Silicon host. The
# container is a throwaway test environment, so the sandbox buys nothing here.
RUN pacman -Syu --noconfirm --needed --disable-sandbox \
      chromium python python-gobject gtk4 xorg-server-xvfb xorg-xauth make \
      openssl ca-certificates \
    && pacman -Scc --noconfirm --disable-sandbox

# Unprivileged user: Chromium refuses to run as root without --no-sandbox, and
# provisioning should be exercised at user scope anyway.
RUN useradd -m -s /bin/bash tester
WORKDIR /src
COPY . /src
RUN chown -R tester:tester /src
USER tester

ENV USUAL_DRY_RUN=1
ENTRYPOINT ["/bin/bash", "test/run.sh"]
