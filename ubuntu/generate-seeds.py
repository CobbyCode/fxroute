#!/usr/bin/env python3
"""Render two explicit Subiquity media configs, never a global NoCloud seed."""
import argparse
import copy
from pathlib import Path

import yaml

AUTOINSTALL_DIR = Path(__file__).resolve().parent / 'autoinstall'


def generate_seeds(output_dir, test_seed=False):
    base = yaml.safe_load((AUTOINSTALL_DIR / 'user-data').read_text())
    if test_seed:
        overrides = yaml.safe_load((AUTOINSTALL_DIR / 'user-data.test').read_text())
        base['autoinstall'].update(overrides['autoinstall'])
        base['autoinstall'].pop('interactive-sections', None)
    output_dir.mkdir(parents=True, exist_ok=True)
    for profile in ('desktop', 'headless'):
        document = copy.deepcopy(base)
        commands = document['autoinstall']['late-commands']
        commands.append(f'echo {profile} > /target/etc/fxroute-iso-profile')
        if profile == 'headless':
            commands.append('curtin in-target --target=/target -- '
                            '/opt/fxroute-iso/scripts/prepare-headless-target.sh')
        # Since Noble, media configs accept a single top-level autoinstall key.
        # No root autoinstall.yaml or cloud-config datasource is staged.
        (output_dir / f'{profile}.yaml').write_text(
            yaml.safe_dump(document, sort_keys=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--test-seed', action='store_true')
    args = parser.parse_args()
    generate_seeds(args.output_dir, args.test_seed)


if __name__ == '__main__':
    main()
