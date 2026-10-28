# Change history

## 0.1.2 (unreleased)

- Run every upload and download through one package-owned helper in a read-only host root, with the sandbox tree as data.
- Remove the conditional dynamic loader preload bind. The helper process no longer uses the writable sandbox root, so the mask is not a boundary.
- Resolve links inside the sandbox root and refuse a directory export that contains a link.
- Check each opened source descriptor before any data read. A pipe, a device, and a socket are refused without a blocking open.
- Publish a download through fresh host inodes below the selected destination, with no link followed there.
- Refuse an existing directory link in a host destination and replace an existing file entry.
- Bound each transfer process with a 300-second deadline.
- Change `Runtime.copy` to `Runtime.transfer` and `Holder.transfer` to take one `Transfer` request.

## 0.1.1 (unreleased)

- Add the MIT license with copyright for 2nd Set AI Corp.
- Add public contributor and security guides.
- Add package source, documentation, and issue links.
- Add one local command for lint, formatting, distributions, and installed-core checks.
- Pin development tools and CI action revisions.
- Test a wheel from the source distribution outside the source checkout.
- Cover a dynamic loader preload file that a seat wrote, so a copy process loads no seat library.
- Accept a download only when each entry is a regular file or a directory.
- Stage a directory download before the host target, so a refused transfer leaves it empty.

## 0.1.0

- Extract the independent Harbor bubblewrap package.
- Support persistent service sandboxes and a defined Compose subset.
- Include four example tasks with Docker and Nix definitions.
- Exercise stock Harbor oracle tasks and negative controls on Linux.
