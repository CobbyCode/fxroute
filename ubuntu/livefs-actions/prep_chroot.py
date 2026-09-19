"""livefs-edit --python action: prepare the squashfs chroot for apt.

The container build exports TMPDIR to a host-backed dir; that variable leaks
into the install chroot where the path does not exist, breaking maintainer
scripts (openssh-server postinst mktemp). Provide the dir inside the chroot
(it lives on the overlay, cleanup_chroot.py removes it after the install).
"""
import os

base = ctxt.edit_squashfs(get_squash_names(ctxt)[0])
tmpdir = os.environ.get('TMPDIR', '/tmp')
if tmpdir.startswith('/'):
    os.makedirs(base + tmpdir, exist_ok=True)
    print('chroot tmpdir ready: ' + tmpdir)
