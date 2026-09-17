"""Register FIRST: livefs-edit runs pre-repack hooks in reverse order.

Upstream context.EditContext.repack rebuilds squashfs/initrd via those hooks,
then invokes xorriso. An ordinary last --python action hashes stale squashfs.
Use the public hook API, not a patched repacker or disabled integrity check.

References: github.com/mwhudson/livefs-editor/blob/main/livefs_edit/context.py
and actions.py (edit_squashfs/unpack_initrd register rebuild hooks).
"""
import hashlib
from pathlib import Path


def final_checksums():
    iso = Path(ctxt.p('new/iso'))
    # Match the stock Ubuntu exclusions: xorriso generates/patches these boot
    # structures AFTER hooks. Ubuntu 26.04.1 uses /boot.catalog (confirmed by
    # xorriso -report_el_torito plain); retain the older boot/grub/boot.cat path.
    # Exact paths only: all payload, seeds, GRUB config, initrd and squashfs
    # files remain covered. md5sum.txt cannot hash itself.
    excluded = {'md5sum.txt', 'boot.catalog', 'boot/grub/boot.cat',
                'boot/grub/i386-pc/eltorito.img'}
    records = []
    for path in sorted(iso.rglob('*')):
        relative = path.relative_to(iso).as_posix()
        if relative in excluded or path.is_symlink() or not path.is_file():
            continue
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'md5').hexdigest()
        filename = './' + relative
        escaped = '\\' in filename or '\n' in filename
        filename = filename.replace('\\', '\\\\').replace('\n', '\\n')
        records.append(('\\' if escaped else '') + digest + '  ' + filename + '\n')
    (iso / 'md5sum.txt').write_text(''.join(records))
    ctxt.log('refreshed md5sum.txt after filesystem rebuilds')


ctxt.add_pre_repack_hook(final_checksums)
