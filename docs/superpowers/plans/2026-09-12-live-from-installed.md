# Live-from-installed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Try FXRoute live squash from a real, working Agama desktop installation instead of the hand-maintained container reimplementation.

**Architecture:** Run the existing `desktop` QEMU install automation to produce a proven-good installed disk, extract its filesystem over SSH (tar, FS-agnostic), apply only true live semantics (volatile identity, no credentials, neutral fstab, ISO-kernel modules, live marker, internal-disk protection), pack a flat squash, and stage it with the existing ISO tooling. The old `build-live-root.sh` reimplementation is deleted.

**Tech Stack:** bash, QEMU/KVM, existing `iso/test-leap-16-iso.sh desktop` flow, tar over SSH, rpm2cpio, depmod, mksquashfs (in container, preserves ownership), dmsquash-live flat format.

**Spec:** User instruction 2026-09-12 (live-from-installed mandate; no install.sh changes; no reimplementation repairs).

## Global Constraints

- `install.sh` is NOT modified; no new installer architecture.
- `FXRoute Desktop` and `FXRoute Headless` flows stay untouched.
- Try boots without Agama/`inst.auto`; all state volatile; hostname `fxroute-live`.
- No credentials or secrets ship in the image (user password, host keys, NM connections, private TLS keys).
- Reproducible squash metadata (`mksquashfs -all-time` epoch floor 86400 — SDDM skips epoch-zero configs, sysusers day-zero expires accounts).
- Every new behavior has a failing test first; artifact tests run against the real built squash.

---

### Task 1: Golden desktop install via existing automation

**Files:**
- Modify: none (uses `iso/test-leap-16-iso.sh desktop` and `dist/fxroute-live-test.iso` as-is)

**Interfaces:**
- Consumes: installer ISO at `dist/fxroute-live-test.iso`, 8 CPUs / 8 GB / 40 GB disk, ~2.5 h unattended.
- Produces: proven-good installed disk at `dist/golden-desktop/desktop.qcow2` (test asserts service, DSP, desktop).

- [ ] **Step 1: Start the golden install in background**

```bash
FXROUTE_ISO_TEST_DIR="$PWD/dist/golden-desktop" FXROUTE_ISO_USER_PASSWORD='GoldenUser123' FXROUTE_ISO_LIVE_PASSWORD='GoldenLive123' FXROUTE_KEEP_ISO_TEST=1 FXROUTE_ISO_TEST_TIMEOUT=7200 FXROUTE_VM_RAM=8192 FXROUTE_VM_CPUS=8 FXROUTE_ISO_LOCALE=de_DE.UTF-8 FXROUTE_ISO_KEYMAP=de FXROUTE_ISO_TIMEZONE=Europe/Berlin ./iso/test-leap-16-iso.sh desktop dist/fxroute-live-test.iso
```

- [ ] **Step 2: Confirm the test passed and the guest is off**

Run: `tail dist/golden-desktop/desktop.log`-equivalent (`dist/golden-desktop.log`); expect `desktop profile passed`. Expect no `qemu-system` process left.

### Task 2: Converter `iso/scripts/build-live-from-installed.sh` (skeleton + fstab/scrub units)

**Files:**
- Create: `iso/scripts/build-live-from-installed.sh`
- Test: `scripts/test_live_from_install.py` (new)

**Interfaces:**
- Consumes: installed disk image + SSH (user `fxroute`, install password), base ISO (kernel RPMs), `iso/scripts/live-udev-nomount.rules`, `iso/scripts/fxroute-live-init.sh` (slimmed, Task 3).
- Produces: `$WORK/live-root/` tree + `$OUTPUT` flat squash.

Converter stages (single script, functions):
1. `boot_installed_disk` — QEMU `-drive disk -boot c`, user-net SSH forward, wait for SSH (no GRUB editing needed; installed boots directly).
2. `scrub_guest_via_ssh` — `passwd -d fxroute`; `rm -f /etc/ssh/ssh_host_*`; `rm -f /etc/NetworkManager/system-connections/*`; `systemctl disable sshd.service caddy.service`; `rm -rf /var/lib/caddy/* /var/log/* ~/.bash_history /root/.bash_history`; `: > /etc/machine-id`; `printf fxroute-live > /etc/hostname`; neutralize `/etc/fstab` (keep pseudo-fs lines only, see fixture test); `poweroff`.
3. `extract_tree` — `ssh fxroute "echo PASS | sudo -S tar -c --one-file-system --numeric-owner [excludes] /" | tar -x -C $TREE` (excludes: proc sys dev run tmp mnt media var/tmp).
4. `apply_live_tree` — live marker files (`/etc/fxroute-live`, build commit), udisks rules copy, slim live-init service install + enable, `fxroute-first-boot.service` mask symlink, sudoers drop-in, live README.
5. `swap_kernel_modules <base-iso>` — extract `kernel-default` + `kernel-default-extra` RPM payloads (`rpm2cpio | cpio -id ./usr/lib/modules`), delete other `/usr/lib/modules/*` and `/boot/*`, `depmod -b $TREE $ISO_KVER` (host depmod, fallback: throwaway Leap container with tree mounted).
6. `install_firmware_set` — `zypper --root $TREE` (throwaway Leap container, tree mounted) install: `kernel-firmware-iwlwifi kernel-firmware-ath10k kernel-firmware-ath11k kernel-firmware-ath12k kernel-firmware-atheros kernel-firmware-brcm kernel-firmware-mediatek kernel-firmware-realtek kernel-firmware-marvell wireless-regdb sof-firmware kernel-firmware-sound kernel-firmware-intel kernel-firmware-bluetooth`.
7. `pack_squash` — throwaway Leap container as root: `mksquashfs /tree $OUTPUT -comp xz -noappend -mkfs-time $EPOCH -all-time $EPOCH -no-xattrs`; chown artifact to host user.

- [ ] **Step 1: Write the failing tests** (`scripts/test_live_from_install.py`):

```python
def test_converter_neutralizes_disk_backed_fstab_entries():
    # fixture fstab with UUID + LABEL + /dev lines plus tmpfs/proc lines
    # run converter's filter function, assert only pseudo-fs lines survive
def test_converter_masks_first_boot_and_disables_sshd():
    # fixture tree: assert mask symlink target /dev/null and no sshd wants link
def test_converter_keeps_iso_kernel_modules_only():
    # fixture modules dirs 6.12.0-160000.35-default + 6.12.0-160000.37.1-default
    # run swap function, assert only .35 remains with modules.dep
def test_iso_builder_calls_new_converter():
    assert "build-live-from-installed" in open("iso/build-leap-16-iso.sh").read()
    assert "build-live-root.sh" not in open("iso/build-leap-16-iso.sh").read()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest scripts.test_live_from_install -v`
Expected: FAIL (script missing)

- [ ] **Step 3: Write minimal implementation** (stages above; fstab filter as a standalone function for the fixture test)
- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest scripts.test_live_from_install -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add iso/scripts/build-live-from-installed.sh scripts/test_live_from_install.py
git commit -m "Add live-from-installed converter with fstab scrub and kernel swap"
```

### Task 3: Slim live-init + live boot options keep

**Files:**
- Modify: `iso/scripts/fxroute-live-init.sh` (trim to: test password hook, ALSA unmute, mpv pre-warm, desktop README; drop Agama masking Hä? NO — keep first-boot mask lines, they are true live semantics against first-boot retry)
- Test: extend `scripts/test_live_from_install.py` (assert slim init keeps prewarm/unmute/password hook)

Keep: `iso/scripts/live-boot-options.txt` (overlay flags, masks incl. `fxroute-first-boot.service`, `fxroute.live=1`), `iso/scripts/live-udev-nomount.rules` (internal-disk protection is true live semantics).

- [ ] **Step 1-5:** test-first trim, run, commit with Task 2 or separately.

### Task 4: Rewire, delete, docs

**Files:**
- Modify: `iso/build-leap-16-iso.sh` (call new converter with `--disk/--base-iso/--output`; drop old builder call)
- Delete: `iso/scripts/build-live-root.sh`, `scripts/test_live_root_builder.py`
- Modify: `scripts/test_live_root_image.py` (keep artifact tests: live marker, NM enabled, ISO-kernel modules, firmware, links, helpers, no credentials (shadow empty, no host keys, no NM connections), neutral fstab, first-boot masked, linger present, session helper)
- Modify: `docs/INSTALL-ISO.md` (new flow: golden install + convert; epoch floor rationale stays)
- Modify: `scripts/test_iso_live_boot.py` (drop `--minimal` builder references if any)

Previous fixes carry over ONLY where live semantics: epoch floor 86400 (SDDM/sysusers), container-side pack (ownership), docker-exclusion of pseudo-fs, live banner (already committed app code), NM enable (installed tree already has it — assert, don't add), kernel-RPM injection (now inside converter), sudoers drop-in (no credentials live).

- [ ] **Step 1-5:** per-file test-first updates, run full live-related suites, commit.

### Task 5: Build, verify, report

**Files:** none (runs only)

- [ ] **Step 1: Full build** (`build-leap-16-iso.sh`, no `--live-squash`): expect exit 0.
- [ ] **Step 2: Artifact tests** `FXROUTE_TEST_LIVE_SQUASH=<staged> python3 -m unittest scripts.test_live_root_image scripts.test_live_from_install`: expect all pass.
- [ ] **Step 3: Boot probe** (existing `/tmp/opencode/radio-probe.sh` pattern against new ISO): expect Try boot, `/api/status` live:true, radio `playing=True`, mpv→DSP sink evidence.
- [ ] **Step 4: Report** image path + proof list. No commit (no code changes).

## Self-Review

- Spec coverage: installed base (Task 1+2), no install.sh changes (none planned), full component carry-over by construction (Task 1 proves base works), live-only deltas enumerated (Task 2 stages 4-5, Task 3), old reimplementation removed (Task 4), new image built (Task 5). BT/SMB runtime mysteries: covered by construction (installed stacks); hotspot observation retested via Task 5 probe + hardware.
- No placeholders: all commands/paths literal.
- Risk: golden install duration (~2 h) and first-boot network fetch; mitigated by KEEP + existing proven automation. Btrfs layout handled by guest-tar extraction (FS-agnostic).

Plan complete and saved to `docs/superpowers/plans/2026-09-12-live-from-installed.md`. Executing inline per your build order.
