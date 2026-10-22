#!/bin/sh
# Install a pinned single-user Nix release into /nix for the current user.
# The Linux dev container uses it to build the Nix definitions of Harbor task
# environments for the bubblewrap backend (docs/spec/harbor-backends.md).
# The caller must own /nix. The script checks the published SHA-256.
set -eu

version=2.34.4
arch="$(uname -m)"
case "$arch" in
    aarch64) sum=11f5dca5e2eccd7fca31d84ee16fbe18e8752c45a9ca223a1e55aaa16548a48b ;;
    x86_64) sum=595669eb6db6117135ca8ba6667b273eaf980a47523a92e61ed963556cd547b8 ;;
    *)
        echo "install-nix.sh: no pinned Nix $version release for $arch" >&2
        exit 1
        ;;
esac

# The upstream installer reads USER, which a container build does not set.
USER="${USER:-$(id -un)}"
export USER

name="nix-$version-$arch-linux"
work="$(mktemp -d)"
curl -fsSL -o "$work/$name.tar.xz" "https://releases.nixos.org/nix/nix-$version/$name.tar.xz"
echo "$sum  $work/$name.tar.xz" | sha256sum -c -
tar -xJf "$work/$name.tar.xz" -C "$work"
"$work/$name/install" --no-daemon --no-channel-add --no-modify-profile
rm -rf "$work"
