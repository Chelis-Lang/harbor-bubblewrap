# A probe environment for the bubblewrap backend tests: the tools that Harbor
# needs, plus Python 3 and tmux. Nixpkgs is the nixos-26.05 branch on 2026-10-15.
# <!-- ste-disable-next-line semicolon sentence-length -->
{ pkgs ? import (builtins.fetchTarball { url = "https://github.com/NixOS/nixpkgs/archive/26f4107e47e3ad0ffd0db220469ba83b6f6d96e8.tar.gz"; sha256 = "0k498brrpk3ckn1ldzyg3hzd4inrz9pzy4vbcci38kncsc13191r"; }) { config = { }; overlays = [ ]; } }:
# <!-- ste-disable-next-line semicolon -->
pkgs.buildEnv { name = "bubblewrap-probe-environment"; paths = [ pkgs.bash pkgs.coreutils pkgs.findutils pkgs.python312 pkgs.tmux pkgs.bubblewrap ]; }
