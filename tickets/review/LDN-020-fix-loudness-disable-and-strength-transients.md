# LDN-020 — Loudness-Disable- und Strength-Transienten beheben

Status: review

## Ziel

Die auf `.104` aufgezeichneten positiven Pegelspitzen beim Abschalten von
Loudness und beim Wechsel zu höherer Strength gezielt beseitigen.

## Umfang

- Beim Abschalten den kanonischen System-Master auf A setzen, bevor Loudness
  gebypasst wird.
- Beim Strength-Wechsel den bestehenden Guard bis zum sicheren DSP-Settle
  halten und erst danach Output Gain weich freigeben.
- Kein Preset-Reload, Graph-/Peak-Monitor-Reconfigure oder Umbau von Formel,
  Stufen, SPL Calibration, Persistenz, UI oder Routing.

## Abnahme

- Lokale Runtime-Tests bestehen.
- Kleiner separater Commit.
- Auf `.104` deployt, Service aktiv.
- Paul prüft anschließend akustisch.

## Umsetzung

- `main.py`: Beim Abschalten wird der kanonische Masterwert aus A vor dem
  guarded Runtime-Wechsel gesetzt. Erst danach darf Loudness gebypasst werden.
  Bei einem Runtime-Fehler wird der 100-%-Masterzustand wiederhergestellt.
- `easyeffects.py`: Der bestehende Output-Gain-Guard bleibt nach dem
  LSP-Volume-Write 350 ms statt 100 ms aktiv, bevor Output Gain weich
  freigegeben wird.
- `scripts/test_loudness_strength_runtime.py`: Produktions-Settle-Fenster
  explizit abgesichert.

## Prüfung und Deployment

- `python3 scripts/test_loudness_strength_runtime.py`: bestanden.
- `python3 -m py_compile easyeffects.py main.py`: bestanden.
- Commit: `9555fb6` (`Prevent Loudness transition peaks`).
- Exakt die committed Fassungen von `main.py` und `easyeffects.py` auf
  `.104` deployed; bereits vorhandene AutoSub-Arbeitsbaumänderungen wurden
  nicht aufgenommen.
- Service aktiv, `/api/easyeffects/extras` HTTP 200 und Main-/Sub-Routing nach
  Neustart vorhanden.
- Kein Push, kein Release. Akustische Abnahme durch Paul steht aus.
