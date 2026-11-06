FROM mcr.microsoft.com/devcontainers/python:1-3.13-bookworm

# Basavbildningens apt-källa för Yarn har en signeringsnyckel som inte längre godkänns
# (NO_PUBKEY 62D54FD4003F6525). Då misslyckas apt-get update, och därmed de
# devcontainer-tillägg som installerar paket (Docker-in-Docker, Java). Yarn används inte här.
RUN rm -f /etc/apt/sources.list.d/yarn.list
