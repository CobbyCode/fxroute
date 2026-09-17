"""livefs-edit --python action: copy the FXRoute payload dir onto the ISO.

livefs-edit --cp handles files only (shutil.copy); the payload is a tree,
so copy it here. Source comes from FXROUTE_UBUNTU_STAGE_DIR (bind-mounted
into the container in docker mode, plain path in direct mode).
"""
import os
import shutil

src = os.path.join(os.environ['FXROUTE_UBUNTU_STAGE_DIR'], 'fxroute-iso')
dst = ctxt.p('new/iso/fxroute-iso')
shutil.copytree(src, dst, dirs_exist_ok=True)
print('payload copied to new/iso/fxroute-iso')
