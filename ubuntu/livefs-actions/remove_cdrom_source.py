"""livefs-edit --python action: drop cdrom apt sources from the live squashfs.

The Desktop live filesystem inherits a file:/cdrom apt source that is
unmounted inside the build chroot, so apt-get update fails with exit 100.
The chroot has archive network access; the cdrom source is not needed.
Runs before --install-packages on the same (cached) squashfs overlay.
"""
import glob

base = ctxt.edit_squashfs(get_squash_names(ctxt)[0])
removed = 0
for path in (
    glob.glob(base + '/etc/apt/sources.list')
    + glob.glob(base + '/etc/apt/sources.list.d/*.list')
):
    with open(path) as fp:
        lines = fp.readlines()
    kept = [line for line in lines if 'cdrom' not in line]
    if len(kept) != len(lines):
        removed += len(lines) - len(kept)
        with open(path, 'w') as fp:
            fp.writelines(kept)
for path in glob.glob(base + '/etc/apt/sources.list.d/*.sources'):
    with open(path) as fp:
        text = fp.read()
    blocks = []
    for block in text.split('\n\n'):
        if 'cdrom' in block:
            removed += 1
            continue
        blocks.append(block)
    with open(path, 'w') as fp:
        fp.write('\n\n'.join(blocks))
print(f'removed {removed} cdrom apt source(s)')
