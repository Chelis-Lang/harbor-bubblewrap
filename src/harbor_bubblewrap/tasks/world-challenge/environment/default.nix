# The bubblewrap backend builds one attribute for each Compose service. The
# Docker backend builds the Dockerfiles instead. Both give bash, coreutils,
# findutils and Python 3, and the world service gets /opt/world from rootfs.
# Nixpkgs is the nixos-26.05 branch on 2026-10-15.
# <!-- ste-disable-next-line semicolon sentence-length -->
{ pkgs ? import (builtins.fetchTarball { url = "https://github.com/NixOS/nixpkgs/archive/26f4107e47e3ad0ffd0db220469ba83b6f6d96e8.tar.gz"; sha256 = "0k498brrpk3ckn1ldzyg3hzd4inrz9pzy4vbcci38kncsc13191r"; }) { config = { }; overlays = [ ]; } }:
# <!-- ste-disable-next-line semicolon sentence-length -->
let tools = [ pkgs.bash pkgs.coreutils pkgs.findutils pkgs.python3 ]; files = pkgs.runCommand "world-files" { } "mkdir -p $out/rootfs/opt/world && cp ${./world.py} $out/rootfs/opt/world/world.py && cp ${./health.py} $out/rootfs/opt/world/health.py"; in
# <!-- ste-disable-next-line semicolon sentence-length -->
{ main = pkgs.buildEnv { name = "world-challenge-seat"; paths = tools; }; world = pkgs.buildEnv { name = "world-challenge-world"; paths = tools ++ [ files ]; }; }
