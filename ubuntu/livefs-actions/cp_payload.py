"""livefs-edit --python action: copy trees onto the ISO.

livefs-edit --cp handles files only (shutil.copy). Payload and seed are
trees: payload -> new/iso/fxroute-iso (read by late-commands via /cdrom),
seed -> new/iso/fxroute-seed (desktop.yaml / headless.yaml, selected only
by the matching GRUB entry's subiquity.autoinstallpath kernel argument).
No cloud-init datasource or automatically discovered autoinstall.yaml.
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
