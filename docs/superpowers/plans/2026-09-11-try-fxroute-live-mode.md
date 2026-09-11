# Try FXRoute Live Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ISO bootet zusätzlich `Try FXRoute` als flüchtiges Desktop-Live-System mit RAM-Overlay, ohne Agama.

**Architecture:** Zweites flaches SquashFS `/LiveFX/squashfs.img` (direktes RootFS mit `/proc`, kein nested ext4) wird via vorhandenem dracut-dmsquash (`rd.live.dir=LiveFX`, `rd.live.overlay.overlayfs=1`) mit gleichem Kernel/initrd gebootet; dritter `mkmedia --add-entry` klont Install-Eintrag mit Live-Kernelopts; Live-Root wird per Docker (Leap 16.0, ohne Host-root) aus Profil-Paketen + `install.sh`-Äquivalent vorgebacken.

**Tech Stack:** bash, mkmedia/mkisofs/isohybrid, dracut-dmsquash-live, Docker Leap 16.0, mksquashfs xz, GRUB2, SDDM/KDE Plasma6, PipeWire, Python venv, systemd user units.

**Spec:** User-Auftrag 2026-09-11 (Try FXRoute, nicht-persistent, Install-Wege erhalten, Live-Root vorgebacken, Dracut-Nested-Format beachten, Bootmenü, udisks-Schutz, Live-Hinweis, 6 Abnahmekriterien).

## Global Constraints

- `Try FXRoute` installiert nicht via Agama, kein `inst.auto`, kein Storage-Proposal.
- Alle Live-Änderungen/Logins flüchtig, keine Persistenz.
- `FXRoute Desktop` + `FXRoute Headless` bleiben erhalten.
- Gleicher Kernel + initrd wiederverwenden, sofern Dracut-Pfad sauber.
- Live-Root-Format exakt nach realem `dmsquash-live`-Pfad (flat mit `/proc` ist laut `usr/sbin/dmsquash-live-root` gültig; nested `LiveOS/rootfs.img` nur für Basis-ISO).
- Interne ATA/NVMe nicht automatisch mounten/verändern (nur Live-Pfad ändern).
- Live-Hinweis: `Live Mode — changes and logins are not saved and will be lost after reboot.`
- Reproduzierbar (`SOURCE_DATE_EPOCH=0`, Shims, `touch -h`).

---

### Task 1: Live-Boot-Parameter + GRUB-Infrastruktur

**Files:**
- Modify: `iso/build-leap-16-iso.sh:15-31,178-267`
- Create: `iso/scripts/live-boot-options.txt`
- Test: `scripts/test_iso_live_boot.py` (neu) + `iso/test-leap-16-iso.sh` (Try-Pfad)

**Interfaces:**
- Consumes: `mkmedia --add-entry` Klon-Verhalten (nur `Install Leap`-Eintrag matcht `install|live`), `isoinfo grub.cfg`.
- Produces: `LIVE_BOOT_OPTIONS` String, `Try FXRoute`-Eintrag ohne `inst.auto`, mit `rd.live.dir=LiveFX rd.live.overlay.overlayfs=1 graphical.target systemd.mask=... fxroute.live=1`.

- [ ] **Step 1: Write the failing test**

```python
# scripts/test_iso_live_boot.py
def test_live_boot_options_has_no_inst_auto():
    text = open("iso/scripts/live-boot-options.txt").read()
    assert "rd.live.dir=LiveFX" in text
    assert "rd.live.overlay.overlayfs=1" in text
    assert "graphical.target" in text
    assert "systemd.mask=agama.service" in text
    assert "inst.auto" not in text
    assert "fxroute.live=1" in text

def test_build_script_has_try_entry():
    text = open("iso/build-leap-16-iso.sh").read()
    assert 'Try FXRoute' in text
    assert 'LiveFX' in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest scripts/test_iso_live_boot.py -v`
Expected: FAIL (Dateien fehlen)

- [ ] **Step 3: Write minimal implementation**

`iso/scripts/live-boot-options.txt` eine Zeile:
```
rd.live.dir=LiveFX rd.live.squashimg=squashfs.img rd.live.overlay.overlayfs=1 rd.live.overlay.reset rw systemd.unit=graphical.target systemd.mask=agama.service systemd.mask=agama-dbus-monitor.service systemd.mask=agama-ssh-issue.service systemd.mask=agama-avahi-issue.service systemd.mask=agama-welcome-issue.service systemd.mask=agama-url-issue.service systemd.mask=agama-certificate-issue.service systemd.mask=agama-certificate-issue.path systemd.mask=agama-certificate-wait.service systemd.mask=fxroute-first-boot.service fxroute.live=1
```

`iso/build-leap-16-iso.sh`: `--help` um Live-Hinweis ergänzen, `STAGE_DIR/LiveFX`-Staging, dritter mkmedia-Lauf `Try FXRoute` mit obigen Opts, grub-Asserts um Try erweitern (vorhanden, kein inst.auto, masks, LiveFX nicht in Install-Einträgen).

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest scripts/test_iso_live_boot.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add iso/scripts/live-boot-options.txt iso/build-leap-16-iso.sh scripts/test_iso_live_boot.py
git commit -m "feat(iso): add Try FXRoute live boot options and GRUB entry"
```

### Task 2: Live-Root-Builder (Docker, flat squash, vorgebacken)

**Files:**
- Create: `iso/scripts/build-live-root.sh`
- Create: `iso/scripts/fxroute-live-init.sh`
- Create: `iso/scripts/live-udev-nomount.rules`
- Test: `scripts/test_live_root_builder.py`

**Interfaces:**
- Consumes: `iso/profiles/desktop.jsonnet` Paketliste, `install.sh` Paketlisten (zypper desktop), `iso/scripts/first-boot-install.sh` Desktop-Defaults als Referenz.
- Produces: `/LiveFX/squashfs.img` (flat squash, Root mit `/proc`, `/etc/fxroute-live`, Hostname `fxroute-live`, Live-User `fxroute`, SDDM-Autologin, FXRoute venv+DSP+`fxroute.service` enabled via Symlinks, Helper vorhanden, `sshd` disabled, `fxroute-first-boot` maskiert).

- [ ] **Step 1: Write the failing test**

```python
def test_builder_exists_and_uses_docker_flat_squash():
    text = open("iso/scripts/build-live-root.sh").read()
    assert "docker" in text
    assert "mksquashfs" in text
    assert "LiveFX" in text
    assert "fxroute-live" in text
    assert "/proc" in text  # flat-Format braucht /proc im Squash-Root

def test_live_init_disables_first_boot():
    text = open("iso/scripts/fxroute-live-init.sh").read()
    assert "fxroute-first-boot" in text
    assert "agama" in text.lower()

def test_udev_rule_ignores_internal():
    text = open("iso/scripts/live-udev-nomount.rules").read()
    assert "UDISKS_IGNORE" in text
    assert "ata" in text.lower() or "nvme" in text.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest scripts/test_live_root_builder.py -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

`build-live-root.sh`: Docker Leap 16.0, zypper install aus Desktop-Profil + install.sh-audio/core-Paketen, User `fxroute`, FXRoute-Checkout auf Build-Commit kopieren, venv+DSP bauen (via `install.sh --providers none --with-lan-name --with-caddy --device-name fxroute-live` im Container ohne systemd-Dienste starten, danach Symlinks für `fxroute.service`+PipeWire manuell setzen), SDDM-Autologin, logind/powerdevil/kwallet/welcome/wallpaper/FF-kiosk/udev-Regel/Marker `/etc/fxroute-live`, `machine-id` leeren, `sshd` disable, `mksquashfs -comp xz -mkfs-time $SOURCE_DATE_EPOCH`.

`fxroute-live-init.sh`: systemd oneshot für Live-Boot (nur RAM): maskiere Agama/first-boot, stelle sicher dass PipeWire+fxroute.service als Live-User laufen, kein git-fetch, kein Hostname-Write auf Disk (nur transient via `hostnamectl --transient`).

`live-udev-nomount.rules`: `SUBSYSTEM==block, ENV{ID_BUS}=="ata|sata|nvme", ENV{UDISKS_IGNORE}="1", ENV{UDISKS_AUTO}="0"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest scripts/test_live_root_builder.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add iso/scripts/build-live-root.sh iso/scripts/fxroute-live-init.sh iso/scripts/live-udev-nomount.rules scripts/test_live_root_builder.py
git commit -m "feat(iso): add docker-based flat live root builder"
```

### Task 3: ISO-Build-Integration (Stage LiveFX + reproduzierbar)

**Files:**
- Modify: `iso/build-leap-16-iso.sh:178-267`
- Test: `scripts/test_iso_live_boot.py` erweitern (Stage-Asserts)

**Interfaces:**
- Consumes: Task1-Opts, Task2-Squash (`$LIVE_SQUASH` oder Build via `build-live-root.sh`).
- Produces: Final-ISO enthält `/LiveFX/squashfs.img` + 3 Einträge, `touch -h` + Shims gelten auch für LiveFX.

- [ ] **Step 1: Write the failing test**

```python
def test_build_stages_livefx():
    text = open("iso/build-leap-16-iso.sh").read()
    assert "LiveFX/squashfs.img" in text
    assert "FXROUTE_LIVE_SQUASH" in text or "build-live-root" in text
    assert 'menuentry "Try FXRoute"' in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest scripts/test_iso_live_boot.py -v -k stage`
Expected: FAIL vor Integration

- [ ] **Step 3: Write minimal implementation**

In `build-leap-16-iso.sh`: `LIVE_SQUASH=${FXROUTE_LIVE_SQUASH:-}`; wenn gesetzt kopieren, sonst `build-live-root.sh` aufrufen (mit `--output $STAGE_DIR/LiveFX/squashfs.img`); `mkdir -p $STAGE_DIR/LiveFX`; `touch -h` einschließen; dritter mkmedia-Lauf nach Desktop mit `$(cat live-boot-options.txt)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest scripts/test_iso_live_boot.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add iso/build-leap-16-iso.sh scripts/test_iso_live_boot.py
git commit -m "feat(iso): stage LiveFX squash into ISO"
```

### Task 4: Live-Hinweis in UI/Appliance

**Files:**
- Modify: `main.py` (`/api/status` um `live:true` wenn `/etc/fxroute-live` oder `fxroute.live=1` in `/proc/cmdline`), `static/index.html`, `static/app.js`, `static/style.css`
- Test: `scripts/test_live_notice.py`

**Interfaces:**
- Consumes: `/etc/fxroute-live`, `/proc/cmdline`.
- Produces: Banner `Live Mode — changes and logins are not saved and will be lost after reboot.` in Web-UI + Desktop-Hinweisdatei `~/Desktop/LIVE-MODE-README.txt`.

- [ ] **Step 1: Write the failing test**

```python
def test_status_has_live_flag():
    import ast
    src = open("main.py").read()
    assert "fxroute-live" in src or "fxroute_live" in src
    assert "/etc/fxroute-live" in src

def test_banner_in_frontend():
    assert "Live Mode" in open("static/index.html").read() or "Live Mode" in open("static/app.js").read()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest scripts/test_live_notice.py -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

`main.py`: Helper `is_live_mode()` (Marker oder cmdline), `/api/status` Response um `"live": true/false`.

Frontend: verstecktes `#live-banner` Div in `static/index.html`, CSS in `style.css` (zurückhaltend, gelb/grau), `app.js` fetch `/api/status` -> wenn `live` einblenden.

Live-Root zusätzlich `~/Desktop/LIVE-MODE-README.txt` + Wallpaper-Hinweis.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest scripts/test_live_notice.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add main.py static/index.html static/app.js static/style.css scripts/test_live_notice.py
git commit -m "feat(ui): add live mode notice"
```

### Task 5: QEMU-Live-Abnahme (gezielt)

**Files:**
- Modify: `iso/test-leap-16-iso.sh` (Profil `try`/`live`: ohne Agama-install direkt booten, prüfe `/api/status live:true`, `fxroute_dsp_sink`, overlay `LiveOS_rootfs`, Home-Volatilität, keine internen Mounts)
- Test: manuell `FXROUTE_ISO_LIVE_PASSWORD=... ./iso/test-leap-16-iso.sh try dist/*.iso` + Struktur-Unit-Tests

- [ ] **Step 1: Write the failing test**

Bash/Python-Asserts: Try-Profil existiert, kein `agama install` im Try-Pfad, Checks für overlay + live-Flag + dsp-sink + no-mount vorhanden.

- [ ] **Step 2: Run test to verify it fails**

Run: `./iso/test-leap-16-iso.sh --help` / pytest
Expected: FAIL (kein try-Pfad)

- [ ] **Step 3: Write minimal implementation**

`test-leap-16-iso.sh`: `try` Case, `select_boot_entry try` (down=4?), `wait_for_live` (curl `/api/status`, ssh als Live-User, `findmnt / | grep LiveOS_rootfs`, `touch ~/live-probe && reboot && ! test -f`, `lsblk`-Mount-Check für `/dev/sd|nvme` ohne Mount außer Live-Medium).

- [ ] **Step 4: Run test to verify it passes**

Run: Strukturtests + (wenn möglich) QEMU-Try-Boot
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add iso/test-leap-16-iso.sh
git commit -m "test(iso): add Try live acceptance path"
```

### Task 6: Doku

**Files:**
- Modify: `docs/INSTALL-ISO.md`
- Test: `grep -q "Try FXRoute" docs/INSTALL-ISO.md`

- [ ] **Step 1-5:** Doku-Abschnitt Live (Boot, RAM-Overlay, flüchtig, keine internen Mounts, Hinweis-Text), Commit `docs(iso): document Try FXRoute`.
