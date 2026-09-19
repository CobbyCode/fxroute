"""livefs-edit --python action: remove the chroot TMPDIR after apt.

Counterpart to prep_chroot.py: maintainer-script droppings must not ship
in the live filesystem.
"""
import os
import shutil

base = ctxt.edit_squashfs(get_squash_names(ctxt)[0])
tmpdir = os.environ.get('TMPDIR', '/tmp')
if tmpdir.startswith('/') and tmpdir != '/':
    shutil.rmtree(base + tmpdir, ignore_errors=True)
    print('chroot tmpdir cleaned: ' + tmpdir)
