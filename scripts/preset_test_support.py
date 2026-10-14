# SPDX-License-Identifier: AGPL-3.0-only
"""Shared preset seeding for tests: every bank has its own preset library."""

from audio.filter_banks import owning_bank_ids


def seed_bound_preset(manager, name, state, chain=()):
    """Write ``name`` into the library of every bank ``state`` binds it in.

    Global has one library per output mode. A pair role is seeded under its
    pair bank and under itself, so the fixture need not know the topology.
    """
    for mode, config in state["modes"].items():
        for role, binding in config["banks"].items():
            if name not in binding.values():
                continue
            libraries = [("global", mode)] if role == "global" else [(bank, None) for bank in owning_bank_ids(role)]
            for bank, owner in libraries:
                store = manager.library(bank, owner)
                store.write(name, manager._native_preset(list(chain), layout="surround" if store.surround else None))
