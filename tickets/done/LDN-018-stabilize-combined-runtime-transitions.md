# Ticket: LDN-018

## Project
FXRoute

## Goal
Die gemeinsame Runtime-Logik von Auto Gain und Loudness auf `.104` stabilisieren,
ohne positive Pegelsprünge, Volume-State-Rücksprünge oder Reconfigure-Nebenwirkungen.

## Task
Die realen Request-/Runtime-Pfade für Volume, Loudness an/aus, Strength,
Auto-Gain-Ziel und Auto Gain an/aus diagnostizieren. Danach ausschließlich
diese Übergänge über einen konsistenten, bei Bedarf abgesicherten Direktpfad
führen und auf `.104` fokussiert prüfen.

## Input
- Bestätigte Fixes LDN-013 bis LDN-017
- Kette: Auto Gain → Loudness → Peak Limiter
- Formel und Ziele gemäß Nutzerauftrag
- Bestehende uncommittierte AutoSub-Änderungen müssen unverändert bleiben
- Kein Preset-Reload, Graph-/Peak-Monitor-Reconfigure, doppelter Save, Push oder Release

## Expected Output
- Nachgewiesene Ursachen der drei Regressionen
- Eng begrenzte Codeänderung mit fokussierten Tests
- Separater Commit
- Deployment und Live-Prüfung auf `.104`
- Dokumentierte Live-Ergebnisse einschließlich Persistenz nach Neustart

## Target Path
- `/home/pbclaw/ai/projects/fxroute`
- `paul@192.168.178.104:/home/paul/fxroute`

## Notes
SPL Calibration, Strength-Stufen, Formeln und UI-Struktur nicht neu gestalten.
Main-/Sub-Links und Nutzerzustand schützen.

## Diagnosis
- `/api/volume` rief bei aktivem Loudness
  `EasyEffectsManager.set_loudness_volume_db()` auf. Der dort verwendete
  `copy.deepcopy()`-Aufruf hatte kein zugehöriges `import copy`; der reale
  `.104`-Trace endete deshalb deterministisch mit `NameError` und HTTP 500
  (`volumechange failed`).
- Der gemeinsame LDN-017-Runtime-Helper schrieb Auto-Gain-`target` und
  `bypass` vor der Loudness-Schutzabsenkung. Damit lagen Auto Gain an/aus,
  Target-Wechsel und kombinierte Loudness-Wechsel außerhalb des bewährten
  LDN-014-Guard-Fensters.
- Auch reine Strength-Wechsel schrieben dieselben Auto-Gain-Properties vor
  dem Guard erneut. Bei gleichzeitig aktivem Auto Gain konnte dieser
  ungeschützte Re-Apply den gemeldeten positiven Übergang auslösen, obwohl
  die abschließenden LSP-/Output-Gain-Zielwerte pegelneutral waren.

## Implementation
- `easyeffects.py`: fehlenden `copy`-Import ergänzt.
- `easyeffects.py`: Loudness zuerst auf den bestehenden 18-dB-Guard absenken;
  erst danach Auto-Gain-Target/-Bypass, FFT und LSP-Volume schreiben und den
  Output Gain wie zuvor monoton in maximal 3-dB-Schritten auf das exakte Ziel
  fahren.
- `scripts/test_loudness_strength_runtime.py`: fokussierte Reihenfolge- und
  Persistenztests für Volume, Loudness an/aus, Strength, Auto-Gain-Ziel und
  Auto Gain an/aus sowie den vorher fehlerhaften Volume-Helper.
- Keine Änderung an Formeln, Zielwerten, Strength-Stufen, SPL Calibration,
  UI, Main-/Sub-Routing oder AutoSub-Hunks.

## Validation
- Lokal bestanden:
  - `python3 scripts/test_loudness_strength_runtime.py`
  - `python3 scripts/test_loudness_live_regressions.py`
  - `python3 scripts/test_loudness_integration.py`
  - `python3 scripts/test_spl_calibration.py`
  - `python3 -m py_compile easyeffects.py main.py`
  - `git diff --check`
- Separater Code-/Test-Commit:
  `7c0a1289c27d67ad555ec8cd746ce850fa78fc75`
  (`Guard combined runtime transitions`).
- Deployment: ausschließlich `easyeffects.py` nach
  `paul@192.168.178.104:/home/paul/fxroute`; SHA-256
  `886f8cf73569a90e9369cd43ac8a52bc3f8c1ae2eb5db070f86b52767f4a17e5`.
  Kein Push oder Release.
- Reale `.104`-Requests mit Auto Gain und Loudness gemeinsam:
  - Volume 44→46: HTTP 200; kanonisch
    `volumeDb=-20.234530099105555`, LSP `volume=-5.53453009910555`,
    `outputGain=-14.7`; Summe exakt `volumeDb`.
  - Loudness aus/an: `bypass=true/false`; aus
    `volume=-20.2345300991056`, `outputGain=0`; an wieder exakte gekoppelte
    Summe.
  - Strength Light→Min→Light: Min
    `volume=4.46546990089445`, `outputGain=-24.7`; Light wieder
    `-5.53453009910555/-14.7`; beide Summen exakt `volumeDb`.
  - Target −15→−18→−15: Auto-Gain-Target exakt; bei −18
    `volume=-8.53453009910555`, `outputGain=-11.7`; Summe exakt
    `volumeDb`.
  - Auto Gain aus/an: `bypass=true/false`; aus
    `volume=-13.5345300991056`, `outputGain=-6.7`; an wieder
    `-5.53453009910555/-14.7`; Summe exakt `volumeDb`.
  - Kombinierter Zustand blieb nach Service-Neustart exakt erhalten:
    Auto Gain an, Target −15, Loudness an, Light, FFT 8192,
    `volumeDb=-20.2345300991056` und identische Runtime-Properties.
- Request-Spanne: derselbe FXRoute-PID für alle sechs Szenarien,
  12 guarded Runtime-Logs, 0 Preset-Load/-Reload, 0 Peak-Monitor-Refresh,
  0 HTTP-4xx/5xx und byteidentische `pw-link -l`-Ausgabe vor/nach den
  Runtime-Requests.
- Finale Routing-Prüfung nach Neustart/Restore:
  `ee_soe_output_level` L/R → `fxroute_21_stage1` L/R;
  Helper-Ausgänge 1/2 → Main FL/FR und 3/4 → Sub RL/RR;
  Output Mode `subwoofer-2.2`, Runtime aktiv, `links_configured=true`,
  `last_error=null`.
- Live-Evidence:
  `/home/paul/fxroute/backups/ldn018-live-20260730-194441/`.
- Exakter Nutzerzustand wiederhergestellt und nochmals über Neustart geprüft:
  The Trip pausiert, Volume/System-Master 44, Auto Gain aus, Ziel −12,
  Loudness aus, Strength Light, FFT 8192, kanonisches
  `volumeDb=-21.392839410828756`, unveränderte SPL Calibration
  `requiredAdjustmentDb=13.299999999999997`; Peak-Monitor verfügbar und ohne
  Warnung.

## Review
- Main-Review akzeptiert: Diff ist auf `easyeffects.py` und den fokussierten
  Runtime-Test begrenzt; bestehende AutoSub-Hunks blieben unverändert.
- Loudness-, Live-Regression-, Integrations- und SPL-Tests sowie
  `py_compile` und `git diff --check` erneut bestanden.
- Deployte SHA-256 stimmt mit dem Review-Nachweis überein; FXRoute-Service
  auf `.104` ist aktiv.

## Status
done
