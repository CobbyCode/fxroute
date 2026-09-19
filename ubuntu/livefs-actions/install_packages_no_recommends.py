"""livefs-edit --python action: install live packages without Recommends.

Upstream --install-packages hardcodes apt-get install -y, which pulls
Recommends (docs, extras) into the live squashfs and therefore onto the ISO
and the installed target. This action mirrors it on the same first layer but
passes -o APT::Install-Recommends=false on the command line, so no config
file is written and later apt runs (Subiquity, install.sh, users) keep stock
behavior. Package list comes from FXROUTE_LIVE_PACKAGES (space-separated).

NOTE: this file is inlined into build-ubuntu-iso.sh inside double quotes,
so it must not contain backticks or dollar signs.
"""
import os

from livefs_edit.actions import get_squash_names

packages = os.environ.get('FXROUTE_LIVE_PACKAGES', '').split()
if not packages:
    raise Exception('FXROUTE_LIVE_PACKAGES is empty; refusing an incomplete live layer')

base = ctxt.edit_squashfs(get_squash_names(ctxt)[0])
ctxt.run(['chroot', base, 'apt-get', 'update'])
env = os.environ.copy()
env['DEBIAN_FRONTEND'] = 'noninteractive'
env['LANG'] = 'C.UTF-8'
ctxt.run(['chroot', base, 'apt-get', 'install', '-y',
          '-o', 'APT::Install-Recommends=false'] + packages, env=env)
