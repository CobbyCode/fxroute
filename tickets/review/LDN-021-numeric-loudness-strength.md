# LDN-021 — Numerische Loudness-Strength 1–10

Status: review

## Ziel

Die vier Loudness-Strength-Labels ausschließlich durch ein numerisches
Strength-Feld von 1 bis 10 ersetzen.

## Umfang

- UI-Zahlenfeld mit Pfeilen, `min=1`, `max=10`, `step=1`.
- Offset: `(10 - strength) * (30 / 9)`.
- Bestehende Werte übernehmen: Full→10, Med→7, Light→4, Min→1.
- Gainformel, guarded Runtime-Pfad, Persistenz, Auto-Gain-Kopplung,
  Responsive-Layout und FFT-Control unverändert lassen.
- Separater Commit und Deployment auf `.104`; kein Push/Release.

## Abnahme

- Backend-/Runtime- und UI-Syntaxprüfungen bestehen.
- Bestehender `.104`-Wert wird korrekt numerisch übernommen.
- Service nach Deployment aktiv.

## Umsetzung

- UI: `Strength` ist ein Number-Input mit `min=1`, `max=10`, `step=1`;
  Checkbox, Control-Reihenfolge, FFT-Control und Responsive-Container bleiben
  bestehen.
- Backend: Strength wird als Ganzzahl 1–10 normalisiert; der Offset wird
  ausschließlich als `(10 - strength) * (30 / 9)` berechnet.
- Bestehende Werte werden beim Laden übernommen:
  Full→10, Med→7, Light→4, Min→1.
- Runtime-Guard, pegelneutrale Gainformel, Auto-Gain-Kopplung,
  Volume-Persistenz und SPL Calibration blieben strukturell unverändert.

## Prüfung und Deployment

- Alle zehn Stufen, alle benachbarten Übergänge in beide Richtungen,
  Legacy-Abbildung und konstante Pegelsumme A lokal geprüft.
- Loudness-Integration, Live-Regressionen, SPL Calibration, Python-Compile,
  JavaScript-Syntax und `git diff --check` bestanden.
- Code-Commit: `e336473` (`Add numeric Loudness strength levels`).
- Exakt die committed Fassungen von `main.py`, `easyeffects.py`,
  `static/app.js` und `static/index.html` auf `.104` deployt.
- Der vorhandene Live-Wert `Light` wurde ohne Runtime-Umbau persistent als
  Stufe `4` übernommen; Auto Gain, Loudness, FFT, Volume und Calibration
  blieben erhalten.
- Service aktiv, Extras-Endpoint liefert `strength: 4`, ausgelieferte UI
  enthält das Zahlenfeld und den neuen Cache-Token.
- Kein Push, kein Release.
