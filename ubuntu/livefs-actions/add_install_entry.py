"""livefs-edit action: exactly Try Ubuntu, FXRoute Desktop, FXRoute Headless.

Keep the stock Try block and preamble unchanged in product builds. Only the
FXRoute entries select an explicit seed; never inject a global cloud datasource.
Test-only console/SSH args may be applied to all three entries.
"""
import os
import re

path = ctxt.p('new/iso/boot/grub/grub.cfg')
with open(path) as fp:
    text = fp.read()

pattern = re.compile(
    r'^menuentry "Try or Install Ubuntu" \{\n'
    r'(?:[^\n]*\n)*?\}\n', re.MULTILINE)
match = pattern.search(text)
if not match:
    raise Exception('Try or Install Ubuntu menuentry not found in grub.cfg')

# This action targets the pinned Ubuntu Desktop menu. Do not silently discard
# new upstream entries if the base ISO changes; review the new menu first.
titles = re.findall(r'^menuentry [\"\']([^\"\']+)', text, re.MULTILINE)
if titles != ['Try or Install Ubuntu', 'Ubuntu (safe graphics)',
              'Boot from next volume', 'UEFI Firmware Settings']:
    raise Exception('Unexpected Ubuntu GRUB menu; review before replacing entries')


def add_args(block, arguments):
    lines = []
    linux_count = 0
    for line in block.splitlines(keepends=True):
        if line.strip().startswith('linux '):
            linux_count += 1
            if not re.search(r'\s---(?:\s|$)', line):
                raise Exception('Stock linux line has no installer argument separator')
            line = re.sub(r'(?<=\s)---(?=\s|$)', arguments + ' ---', line, count=1)
        lines.append(line)
    if linux_count != 1:
        raise Exception('Expected one stock linux line')
    return ''.join(lines)


stock_entry = match.group(0)
extra = os.environ.get('FXROUTE_GRUB_TEST_EXTRA', '').strip()
try_entry = add_args(stock_entry, extra) if extra else stock_entry
entries = [try_entry]
for profile in ('desktop', 'headless'):
    arguments = ('autoinstall '
                 f'subiquity.autoinstallpath=cdrom/fxroute-seed/{profile}.yaml')
    if extra:
        arguments += ' ' + extra
    entry = stock_entry.replace('"Try or Install Ubuntu"',
                                f'"Install FXRoute {profile.title()}"', 1)
    entries.append(add_args(entry, arguments))

# Drop the safe-graphics and firmware utility menu tail deliberately: the
# requested product menu has exactly three entries. Stock Try remains first.
with open(path, 'w') as fp:
    fp.write(text[:match.start()] + ''.join(entries))
print('added FXRoute Desktop and Headless GRUB entries')
