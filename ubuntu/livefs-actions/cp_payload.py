"""livefs-edit --python action: copy trees onto the ISO.

livefs-edit --cp handles files only (shutil.copy). Payload and seed are
trees: payload -> new/iso/fxroute-iso (read by late-commands via /cdrom),
seed -> new/iso/fxroute-seed (read by cloud-init via
ds=nocloud;seedfrom=file:///cdrom/fxroute-seed/ on the kernel cmdline;
the squashfs seed dir is shadowed by empty upper-layer placeholders).
Sources come from FXROUTE_UBUNTU_STAGE_DIR (bind-mounted in docker mode).
"""
import os
import shutil

stage = os.environ['FXROUTE_UBUNTU_STAGE_DIR']
copies = (
    (os.path.join(stage, 'fxroute-iso'), ctxt.p('new/iso/fxroute-iso')),
    (os.path.join(stage, 'seed'), ctxt.p('new/iso/fxroute-seed')),
)
for src, dst in copies:
    shutil.copytree(src, dst, dirs_exist_ok=True)
    print('tree copied to ' + dst)
