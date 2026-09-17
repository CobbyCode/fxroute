"""livefs-edit --python action: write the autoinstall seed into the live stack.

The Desktop ISO stacks squashfs layers at boot with the .live layer on top;
that layer ships EMPTY seed placeholders which shadow anything placed in a
lower layer (cloud-init then reads 0 bytes and subiquity stays interactive).
So the seed (user-data + meta-data from the stage dir) goes into the
topmost live layer (name containing .live), falling back to layer [0].
"""
import os

stage = os.environ['FXROUTE_UBUNTU_STAGE_DIR']
names = get_squash_names(ctxt)
live = [name for name in names if '.live' in name]
target = live[0] if live else names[0]
base = ctxt.edit_squashfs(target)
seed = base + '/var/lib/cloud/seed/nocloud'
os.makedirs(seed, exist_ok=True)
for name in ('user-data', 'meta-data'):
    with open(os.path.join(stage, name), 'rb') as src:
        content = src.read()
    with open(os.path.join(seed, name), 'wb') as dst:
        dst.write(content)
print('seed written to layer ' + target)
