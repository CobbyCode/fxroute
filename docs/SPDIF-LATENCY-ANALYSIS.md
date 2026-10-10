# S/PDIF-Decoderlatenz: Ursachen, Messungen, Reduzierungsoptionen

**Aktueller Stand:** Die folgende Analyse beschreibt den ursprünglichen
8192-Frame-Pfad. Die Umsetzung vom 09.10.2026 und ihre neuen Messbelege stehen
in Abschnitt 10: 3074 Frames feste Live-Latenz (64,04 ms), 2050 Frames
Erkennungsfenster, 1024 Frames Worker-Marge. Alte Messwerte in Abschnitten 1–9
sind historische Vergleichswerte, keine aktuellen Produktkonstanten.

Analyseauftrag vom 09.10.2026: warum der PCM-/AC-3-/DTS-Eingangspfad 8192
Samples (170,67 ms bei 48 kHz) puffert, und wie weit sich das zuverlässig
reduzieren lässt. Nur Analyse und Messungen; keine Produktänderungen.

Ergänzt `docs/DSP-LATENCY-REPORT.md` (Abschnitt 6) und beantwortet dessen
offenen Punkt „Decoder-Zeitraster" teilweise. Messartefakte und Grenzen sind
explizit ausgewiesen.

## 1. Architektur und Ursachen der Puffergrößen

Der Ingress (`native_surround/`) besteht aus drei Zeitbasen:

| Baustein | Wert | Ort | Zweck |
|---|---:|---|---|
| `SURROUND_LOOKAHEAD` | 4096 Frames (85,33 ms) | `surround.h:9`, Emit-Bedingung `surround.c:406-409` | Erkennungs-Fenster: Frame N wird erst emittiert, wenn N+4096 Trägerframes eingelesen sind |
| `SURROUND_LIVE_LATENCY` | 8192 Frames (170,67 ms) | `surround.h:15`, Pull `ingress.c:119-125` | Live-Verzögerung: RT-Callback liest Frame `seq − 8192` aus der Output-Queue |
| Worker-Marge | 8192 − 4096 = **4096 Frames (85,33 ms)** | implizit: Differenz der beiden Konstanten | Scheduling-Puffer für den Decode-Worker (1-ms-Poll, FFmpeg-Burst-Decode, OS-Scheduling) |

Der Worker (`ingress.c`, `decode_worker`) emittiert Frame N, sobald
`surround_push` ihn nach dem Lookahead übergibt (≈ Input N+4096). Der
RT-Callback verbraucht ihn bei `seq = N+8192`. Jeder Frame liegt also
4096 Frames in der Output-Queue, bevor er gespielt wird: fällt der Worker
zurück, überbrückt diese Marge die Verzögerung; „late" Frames werden verworfen
und durch Stille ersetzt (fail closed), Queue-Überlauf setzt den Decoder per
`surround_reset` zurück.

### Warum 4096 Frames Lookahead?

Der Lookahead ist das **Entscheidungsfenster der Erkennung**: ein Frame kann
bis dahin noch nachträglich

- dekodiert werden (AC-3/DTS-Bursts werden bei `burst_frame` rückwirkend in den
  Pending-Ring geschrieben, `surround.c:decode()`),
- stumm geschaltet werden (`mute_pending`, `bad_burst`, `unknown`) —
  Null-/Pause-Bursts, defekte Bursts (CRC, Header-Error), unbekannte Träger,
  Roh-DTS-Treffer.

Ein Frame M ist entschieden, sobald Input > M + max. Burst-Dauer + Präambel-Lag
hinausgelaufen ist. Die längsten unterstützten Bursts sind DTS Typ 0x0d mit
2048 Frames (`duration()`, `surround.c:104-112`; AC-3: 1536, DTS: 512/1024/2048,
nicht unterstützte Typen werden bereits im Header gemutet). Der theoretisch
nötige Lookahead liegt damit bei **≈ 2050 Frames**; 4096 konservativ das
Doppelte. `DECODE_SAMPLES 4096` und `mute_pending` (bis `input + 4096`) nutzen
diesen Spielraum grob aus, ohne ihn zu erzwingen.

### Warum 4096 Frames Marge?

Der Decode-Worker läuft außerhalb des RT-Kontexts: 1-ms-`nanosleep`-Poll über
die SPSC-Queues (32768 Frames), FFmpeg-Decode je Burst, Status-Write je
≈100 ms. Die Marge ist eine reine **Scheduling-Reserve** ohne algorithmische
Notwendigkeit — sie ist der Anteil, der sich am ehesten verkleinern lässt,
wenn man die tatsächlich beobachtete Worker-Verzögerung kennt (heute wird sie
nicht gemeldet; siehe Risiken).

### Codecabhängige Latenz: keine

AC-3 und DTS sind frame-synchron (ein Burst hinein = ein Frame heraus,
1:1-Abbildung, `docs/DSP-LATENCY-REPORT.md` 6.1) und laufen durch denselben
Live-Pfad. Es gibt **keine codecabhängige Zusatzverzögerung** — die
Untergrenze pro Burst ist die Framedauer des Formats (AC-3 32,0 ms, DTS 512
10,67 ms), weil ein Burst erst mit seinem Payload vollständig decodierbar ist.

## 2. Messungen auf `.104` (09.10.2026)

### Aufbau

Signalweg: `Mini-PC → HDMI → TCL TV (Auto) → S/PDIF → Scarlett 16i16,
Eingang 9/10` (`capture_AUX8/9`) → `fxroute_surround_input` → DSP.

Der Fernseher lieferte zum Messzeitpunkt **digitale Stille** (Spitze 0,002 LSB,
keine IEC-Präambeln), daher wurde der Carrier **injiziert**: ein generierter
29-s-Testcarrier (PCM-Rauschen + AC-3 640k + DTS 1411,2k via ffmpeg
`-f spdif`, definierte Formate in einer Datei) lief über `pw-cat` zusätzlich in
`carrier_L/R` (der stumme TV-Link blieb verbunden und wurde als Tap mit
aufgezeichnet). Ein einziger 14-Kanal-`pw-cat`-Recorder nahm Quell-Tap,
TV-Tap und alle 10 Helfer-Outputs in einem Callback-Takt auf; die Auswertung
ist frame-genau (Wort-Matching der Carrier-Wort-Streams).

Der Injektionspfad umgeht den TV. Er misst den **Helfer und den Graphen** exakt
(Abschnitt 5 listet, was der TV-Weg zusätzlich hergibt). Messdaten und Skripte:
`/tmp/opencode/spdif-latency/` auf `.104` (`run_capture.sh`, `gen_source.py`,
`analyze2.py`, `blockscan.py`, `record_idle.f32`, `record_stress.f32`).

Gemessen auf dem Produktionsgraphen (Quantum 1024 = 21,33 ms, Service lief).

### Ergebnis A: Leerlauf, 8 Schalter

| Übergang | Messung (Frames @48 kHz) | ms | Erwartung aus dem Code |
|---|---:|---:|---|
| Transport Helfer (PCM, konstant über 29 s) | **8192 + 1024** | 192,0 | 8192 algorithmisch + 1 Graph-Hop des Beobachters |
| PCM → AC-3 (2×) | geschluckter PCM-Tail **4096** | 85,3 | Lookahead-Wipe `mute_pending` |
| PCM → DTS | 4096 + 32 | 85,3 + 0,7 | 32 = Sinus-Phase des Testquellsignals |
| AC-3 → PCM | **10754** Stille | 224,0 | 12288 − 1536 + 2 (`SURROUND_SYNC_TIMEOUT`) |
| DTS → PCM | **11778** Stille | 245,4 | 12288 − 512 + 2 |
| AC-3 stereo → AC-3 5.1(side) | **0** | 0 | Layout-Wechsel mit `avcodec_flush_buffers`, sample-genau |
| AC-3 → DTS (direkt) | ≤ 32 (Quell-Artefakt) | ≤ 0,7 | Burst-Fortsetzung ohne Lücke |

Weiter bestätigt: alle Formate und Layouts wurden von der Status-API korrekt
gemeldet (`codec`, `layout`, `state`), `errors` blieb unverändert; die
Kanalprojektion stimmt (5.1(side) belegt SL/SR, BL/BR still; AC-3 stereo nur
FL/FR); **keine einzige** stille Stelle außerhalb der Schaltvorgänge — der
Worker hat bei 4096 Frames Marge keinen Frame verloren.

### Ergebnis B: CPU-Last (6 Busy-Loops auf 4 Kernen)

- Alle Bursts dekodiert, keine Decodierfehler, Quell-Tap lückenlos, **keine
  Inhalts-Frames verloren** — auch unter Last trägt die Marge.
- Der Beobachterpfad (nicht-RT `pw-cat`-Recorder) springt unter Last blockweise
  um ±1024 Frames (Messlatenz-Tapp wobbelt 8192↔9216, 315 Blöcke) — ein
  **Messartefakt** der Graph-Scheduling-Reihenfolge von Recorder vs. Helfer,
  nicht des Helfers (Inhalts-Bytes stimmen zeitgleich exakt). Dadurch
  erscheinen Schalt-Stillen im Messsignal um ±1 Quantum verkürzt/verlängert
  (z. B. 3584 statt 4096 Frames); die Ursache wurde frame-genau identifiziert
  („Inseln" sind pünktlicher PCM-Inhalt bei k=8192).
- Implikation für den Produktionspfad: jeder PipeWire-Link-Hop kostet am
  Quantum 1024 bis zu 21,3 ms. Capture → DSP-Eingang rechnerisch
  8192 + 2 Hops ≈ 10 240 Frames ≈ **213 ms**; die AV-Sync-Gutschrift (8192)
  bildet die Hops nicht ab (bekannte Lücke, `docs/DSP-LATENCY-REPORT.md` 6.2).

### Live-Messung der Geräuschschutzmechanismen

Vor den Messruns zeigte der reale TV-Träger einen `frame CRC mismatch`
(`errors=1`, FFmpeg-Prüfung `AV_EF_CRCCHECK|BITSTREAM|BUFFER|EXPLODE`): ein
defekter AC-3-Burst wurde gemutet statt verrauscht durchgereicht — der
Schutzpfad arbeitet im Live-Betrieb wie entworfen.

## 3. Schutz vor Digitalrauschen (Übersicht)

| Mechanismus | Wirkung | Latenzbezug |
|---|---|---|
| Null-/Pause-Bursts (IEC-Typ 0/3) | Payload wird nicht als Audio und nicht als verschachtelter Stream gelesen | keiner |
| Header-Error-Bit, korrupter Payload, CRC-Fehler | Burst-Region `burst_frame…+duration` stumm (`bad_burst`) | braucht das Lookahead-Fenster als Retro-Mute |
| Unbekannte Trägertypen | Region stumm, Payload wird nicht gescannt | Entscheidung im Header, kein Lookahead nötig |
| Roh-DTS-Lookalike (TV-PCM trifft −225/+232 ~1×/min) | Mute erst nach 3 Wiederholungen mit konstantem Abstand | Retro-Mute nur bis `emitted`; bereits emittierte Frames sind unveränderbar |
| Mute-Latch | endet bei gültigem Burst, 8192 exakten Null-Frames (`SURROUND_SILENCE_RESET`) oder 12288 Frames ohne Präambel (`SURROUND_SYNC_TIMEOUT`, 2× max. E-AC-3-Burstperiode 6144) | bestimmt die Bitstream→PCM-Lücke |
| Nicht endliche / übersteuerte Samples | `unknown("corrupt")` | — |
| Clock/Queue/Link-Fehler | fail closed: Stille, Reset, kein Verrauschen | — |

**Wichtig:** das Retro-Mute-Fenster ist heute exakt der Lookahead (4096).
Frames in der Output-Queue (die 4096-Frames-Marge) werden beim Mute nicht
zurückgenommen. Ein kleinerer Lookahead verkürzt dieses Fenster — Ausgleich
siehe Vorschlag (Queued-Watermark).

## 4. Formatwechsel: Kosten heute

| Wechsel | Kosten | Ursache |
|---|---:|---|
| PCM → Bitstream | 85,3 ms PCM-Tail verschluckt | `mute_pending` löscht ab `emitted`, also auch **gültiges PCM vor** dem Burst — Implementierungswahl, nicht fundamental (`bad_burst` setzt den Start korrekt auf `burst_frame`) |
| Bitstream → PCM | 224 ms (nach AC-3) bzw. 245 ms (nach DTS) Stille | `SURROUND_SYNC_TIMEOUT 12288` minus letzte Burst-Dauer |
| AC-3 ↔ DTS, Layout-Wechsel | 0 | Burst-Fortsetzung, Decoder-Flush |

## 5. Was ist messbar — TV-Weg (Mini-PC → TCL → S/PDIF)?

- **Vom Scarlett-Eingang an aufwärts frame-genau messbar:** die
  Carrier-Aufzeichnung (AUX8/9) ist selbst die Referenz — Formaterkennung,
  Burst-Geometrie, ob der TV AC-3/DTS bitgenau durchreicht oder re-encodet,
  Schalt-Lücken des TVs, Pausenverhalten (Null-Bursts vs. digitale Stille),
  und exakt die Helfer-Metriken wie in Abschnitt 2 (Kopplung von
  Carrier-Frame-Index zu Helfer-Ausgabe).
- **Nicht von `.104` allein messbar:** die Passthrough-Latenz des TVs
  (HDMI-Eingang → S/PDIF-Ausgang) und die Render-Zeit des Mini-PC — es fehlt
  eine Referenz auf der Quellseite. Für eine Gesamt-AV-Bilanz braucht es einen
  zweiten, synchronen Messpunkt am Mini-PC (z. B. Klick-Signal parallel über
  HDMI und eine aufgezeichnete Referenz) oder man akzeptiert die TV-Latenz als
  unbekannte Konstante.
- **Praktikable Durchführung:** gleicher Recorder-Aufbau wie Abschnitt 2, aber
  ohne Injektion; Testdateien (PCM/AC-3/DTS + Schnellwechsel) laufen am
  Mini-PC. Alle Schaltzeitpunkte stehen frame-genau im Carrier; benötigt
  Bedienung des Mini-PC (oder Zugriff darauf).
- Offen und nur so messbar: verhält sich der TV-Ruhezustand als fortlaufende
  Null-Bursts (dann hält `last_sync` den Mute-Latch offen) oder als reine
  PCM-Nullen (dann greift `SURROUND_SILENCE_RESET`)? Das entscheidet, welche
  der beiden Konstanten die reale Bitstream→PCM-Lücke bestimmt.

## 6. Reduzierungsoptionen

| # | Maßnahme | Effekt | Aufwand/Risiko |
|---|---|---|---|
| 1 | `SURROUND_LIVE_LATENCY` 8192 → Lookahead + kleine Marge, z. B. **2050 + 1024 = 3074 Frames (64 ms)** oder aggressiv 2050 + 512 = 2562 (53,3 ms) | −107…−117 ms | Marge muss zur Worker-Verzögerung passen (Risiko: Aussetzer unter Last) |
| 2 | Lookahead 4096 → **≈ 2050** (max. Burst-Dauer 2048 + Lag) | PCM-Tail-Schluck 85,3 → 42,8 ms; Retro-Mute-Fenster schrumpft | gering; Grenze ist die Entscheidung für korrupte Bursts |
| 3 | `mute_pending` bei PCM→Bitstream nur ab `burst_frame` löschen (wie `bad_burst`) | PCM-Tail-Kosten **≈ 0** statt 85,3 ms | kleine, lokal begrenzte Änderung |
| 4 | Retro-Mute-Watermark in die Output-Queue (Frames bis zum Pull-Punkt zurücknehmen) | größerer Geräuschschutz-Fenster (Lookahead + Marge) ohne Latenzkosten | kompensiert #2 |
| 5 | `SURROUND_SYNC_TIMEOUT` 12288 → **≥ 6656** (6144 + eine Burstperiode) | Bitstream→PCM-Lücke 224/245 → ~107/128 ms | Fallback in E-AC-3-Streams muss ausgeschlossen bleiben |
| 6 | `SURROUND_SILENCE_RESET` 8192 → kleiner (z. B. 2048–6400) | verkürzt die Lücke bei TV-Stille in Nullen | erst nach TV-Messung (Abschnitt 5) sinnvoll festlegbar |
| 7 | Quantum des Carrier-Graphen senken (z. B. 256) | Graph-Hops 21,3 → 5,3 ms; AV-Bilanz ehrlicher | betrifft den ganzen Graphen; separate Betrachtung |
| 8 | Optional: entscheidungs-adaptive Emission (Frame emittieren, sobald entschieden) | AC-3 ~32 ms, DTS-512 ~10,7 ms, Worst Case 42,7 ms | variable Latenz erschwert den AV-Sync-Credit; nur mit fester Zusatzzusage beherrschbar |

**Realistisches Ziel mit #1–#3, #5:** ca. **64 ms** feste Ingress-Latenz
(heute 170,7 ms) und Schaltkosten nahe 0 (PCM→Bitstream) bzw. ~107–128 ms
(Bitstream→PCM). Darunter (≈ 53 ms) nur mit belegter Worker-Marge.

## 7. Risiken

1. **Lookahead unter der Burst-Entscheidung (< ~2050):** korrupte Bursts werden
   hörbar (Rauschen), bevor `bad_burst` greifen kann — die harte Untergrenze.
2. **Zu kleine Worker-Marge:** Aussetzer (Stille) unter CPU-Spitzen. Die
   Messung belegt Robustheit bei 4096 Frames auch bei 150 % CPU-Last, zeigt
   aber nicht, wie viel Reserve tatsächlich verbraucht wird — dafür bräuchte
   es Worker-Latenz-Telemetrie (fehlt heute). Vor Verkleinerung unter
   ~2048 Frames: Telemetrie ergänzen oder Lasttests mit Schnellwechseln.
3. **`SURROUND_SYNC_TIMEOUT` < 6144:** falscher PCM-Fallback mitten in
   E-AC-3-Streams (max. Burstperiode 6144 Frames) → Rauschen.
4. **AV-Sync:** `LIVE_LATENCY_FRAMES` (`audio/digital_input.py:20`) und
   `demo/routes.js:808` müssen mitziehen, sonst verschiebt sich Lippsync um
   die Änderung; die Docs (`MANUAL.md`, `docs/surround.md`) nennen 170,67 ms.
5. **Graph-Hops:** Die Link-Hops (je 1 Quantum) sind reale Latenz und werden
   nicht gutgeschrieben; bei Quantum 1024 sind das bis zu 42,7 ms zusätzlich.
6. **Roh-DTS-Spätmeldung:** Die 3-Sync-Regel erkennt erst spät; bei kleinerem
   Lookahead wird das Retro-Mute-Fenster dünner — Maßnahme #4 gleicht das aus.

## 8. Vorschlag (zuverlässig, minimale Latenz)

1. **Konstanten neu dimensionieren:** Lookahead 4096 → 2050; Live-Latenz
   8192 → 3074 (2050 + 1024 Marge) als konservative erste Stufe, AV-Sync-Credit
   und Status-Reporting (`lookahead_frames`, `live_latency_frames`) mitziehen.
2. **Mute-Fenster korrigieren:** `mute_pending` bei PCM→Bitstream ab
   `burst_frame` statt ab `emitted` (Kosten → ≈ 0), plus Retro-Mute-Watermark
   in die Output-Queue (Schutz-Fenster bleibt ≥ heute).
3. **`SURROUND_SYNC_TIMEOUT` 12288 → 6656**, `SURROUND_SILENCE_RESET` erst
   nach TV-Messung anpassen.
4. **Worker-Latenz-Telemetrie** (max. beobachtete Queue-Verzögerung, Anzahl
   verworfener „late"-Frames) aufnehmen und mit den vorhandenen
   Live-Tests (`native_surround/test_live.py`) eine Stressvariante mit
   Formatschnellwechseln unter Last fahren; damit wird die Marge von 1024
   belegt statt behauptet.
5. Danach — bei belegter Marge — zweite Stufe auf **2562 Frames (53,3 ms)**
   oder entscheidungs-adaptiv (Abschnitt 6, #8).

## 9. Grenzen dieser Analyse

- Injektionsmessung: TV-Passthrough fehlt (Abschnitt 5); der reale
  Fernseher kann zusätzliche Latenz und andere Schalt-Lücken haben.
- Der Beobachterhop (±1024 Frames unter Last) ist Messpfad-Jitter; exakt
  gemessen ist die algorithmische Verzögerung 8192 (konstant, beide Runs).
- Die Worker-Marge wurde nicht ausgereizt, daher ist ihr tatsächlicher Bedarf
  eine Abschätzung aus Code und Lastverhalten, keine Grenzwertmessung.
- Nur 48 kHz; Roh-DTS wird nicht dekodiert (Produkt-Grenzen, unverändert).

## 10. Umsetzung und Verifikation (09.10.2026)

> Korrektur 10.10.2026 (Latenz): Eingangsdelay 3074 → **2818 Frames (58,71 ms)**,
> Worker-Marge 1024 → **768 Frames (16,00 ms)**, Graph-Quantum **768** (vom Helfer
> erzwungen, bei Stopp restauriert). 3071 Frames (63,98 ms) mit Quantum 1024
> wurden gemessen und verworfen: 3 Frames systematisches Late/Underrun pro
> 1024er-Block (0,29 %). Quantum 256 wurde wegen dokumentierter DSP-Instabilität
> nicht übernommen. Private Graphen (PCM/AC-3/DTS, Stereo/diskret,
> Schnellwechsel mit/ohne Last, 2048-Quantum-Prevention) sind verlustfrei mit
> positiver Reserve; siehe Staging-Artefakte. Historische 3074er-Messwerte unten
> bleiben für die alte Fassung gültig.

> Korrektur 10.10.2026: Reale TCL-Wechsel zeigen verwaiste AC-3-Payload-Präfixe
> nach der Lücke. Deshalb löscht eine neu erkannte Präambel wieder das gesamte
> noch ausstehende Präfix ab dem frühesten unemittierten Frame (bis zu
> 42,71 ms PCM-Verlust unmittelbar vor dem Wechsel); nur früheres PCM bleibt
> pegelgetreu. Die darunter beschriebene Erhaltung etablierten PCM-Tails vor der
> Präambel gilt nicht mehr.

### Aktuelle Regeln

| Größe | Neu | Wirkung |
|---|---:|---|
| Erkennungsfenster | 2050 Frames | Deckt 2048-Frame-DTS plus Cross-Frame-Präambel ab |
| Feste Live-Latenz | 3074 Frames = **64,04 ms** | Alle zehn Outputs, PCM/AC-3/DTS identisch; −106,625 ms |
| Worker-Marge | 1024 Frames = 21,33 ms | Quantum-abhängige Scheduling-Frist, keine zusätzliche Codec-Latenz |
| Sync-Timeout | 6656 Frames | Bleibt über maximaler E-AC-3-Periode 6144 |
| Bestätigtes Roh-DTS | 8704 Frames ohne Sync | Deckt akzeptierte Sync-Abstände bis 8192 Frames ab |
| Exakte Null-Stille | 8192 Frames, unverändert | Kein unbelegtes Verkürzen des TV-Stille-Resets |

Etabliertes PCM→IEC löscht nur noch ab dem ersten Präambel-Frame, auch wenn
die Präambel am rechten Port beginnt. Alle davor liegenden etablierten
PCM-Frames bleiben erhalten. Der allererste Präfix wird erst nach dem
2050-Frame-Erkennungsfenster als PCM etabliert: Liegt vorher eine Präambel,
wird dieser unklassifizierte Präfix als mögliches Startup-Payload gemutet.
Das schützt einen mitten im AC-3/DTS-Payload beginnenden Eingang. Ein sehr
kurzer initialer PCM-Präfix vor dem ersten Burst bleibt dabei aus
Sicherheitsgründen ebenfalls stumm; reines PCM ist weiterhin ab Frame null
pegelgetreu. Das ist keine zusätzliche Verzögerung im etablierten Stream.
AC-3↔DTS und Layoutwechsel behalten ihre sample-genaue Abbildung. PCM nach
dem letzten Burst beginnt bei letzter Präambel +6658 Frames (die bestehende
Reihenfolge aus Latch-Ende und nächstem PCM-Schreiben bleibt unverändert):

- AC-3: **5122 Frames / 106,71 ms** nach Burst-Ende statt 10754 / 224,04 ms.
- DTS-512: **6146 Frames / 128,04 ms** statt 11778 / 245,38 ms.

Diese verbleibende Lücke ist Geräuschschutz, keine Worker-Verzögerung. Ohne
zusätzliche Kenntnis des Quellformats darf ein laufender unbekannter IEC-Stream
nicht innerhalb seiner maximalen Burst-Periode zu PCM werden. Regelmäßige
6144-Frame-E-AC-3-Präambeln, Null-/Pause-Bursts und ausgeschlossene Payload-
Scans halten den Schutz weiterhin offen; CRC-/Header-/Decoderprüfungen bleiben.
Bestätigtes Roh-DTS verwendet separat 8704 Frames Timeout: Die Erkennung
akzeptiert Sync-Abstände bis 8192 Frames, die nicht vom kürzeren IEC-Timeout
unterlaufen werden dürfen. Eine IEC-Präambel löscht diese Roh-DTS-Historie,
damit AC-3/DTS→PCM wieder den kürzeren IEC-Timeout verwendet.
Die bestätigte Roh-DTS-Latch ist getrennt von der Kandidatenhistorie: Bei
Wechsel von Sync-Abstand oder Packing halten plausible Syncs im akzeptierten
Abstandsbereich die Unterdrückung während der erneuten Qualifikation offen.
Eine initiale Erkennung benötigt weiterhin drei passende Wiederholungen;
ein isolierter PCM-Lookalike aktiviert die Latch nicht.

### Queue-Schutz und Grenzen

Bei breitem Safety-Mute (bestätigtes Roh-DTS, korrupte Samples, Reset) setzt der
Worker einen monotonen **absoluten Frame-Watermark**. Der RT-Pull unterdrückt
noch nicht gepullte Frames darunter. Kein Thread überschreibt dabei Queue-
Slots des anderen. Später gültige Bursts oberhalb des Watermarks bleiben
spielbar; verpasste Pull-Fristen werden nie als zeitversetztes Audio nachgeholt.

**Korrektur zur Formulierung „Schutz-Fenster bleibt ≥ heute“ in Abschnitt 8:**
Mit insgesamt 3074 Frames kann kein Verfahren ein altes 4096-Frame-
Retrofenster vollständig erhalten. Der Watermark erweitert das neue
2050-Frame-Pending-Fenster bis zum tatsächlichen Pull-Punkt, maximal 3074
Frames; bereits gepullte Frames sind unveränderbar. Bei spät bestätigtem
Roh-DTS mit großen Sync-Abständen bleibt deshalb ein unerkannter Präfix möglich.
Die Drei-Sync-Regel und bitgenaues Durchlassen isolierter PCM-Lookalikes bleiben
erhalten. Payload vor einer allerersten Präambel außerhalb des Startup-Fensters
ist weiterhin nicht sicher von PCM unterscheidbar. Das ist keine Zusage für beliebige
unbekannte Bitstreams; unterstützte IEC-AC-3/DTS-Bursts bleiben geschützt.

Ein Graph-Quantum **über 1024** kann die 3074-Frame-Zeitachse nicht zuverlässig
bedienen: Die Erkennungsentscheidung einiger Samples würde erst aus dem
gleichen Callback stammen, der sie bereits ausgeben muss. Der Helfer liefert
dann fail-closed Stille. Er verändert das Graph-Quantum nicht; der geprüfte
Produktgraph auf `.104` bleibt bei 1024. Nicht-48-kHz-Graphen bleiben stumm.

### Worker-Telemetrie

Der RT-Callback versieht jeden Inputblock mit monotonic timestamp, Block-
Startindex und Quantum. Nach Erkennung/Decode/Emission misst der Worker:

- `worker_delay_max_us`: größte Callback→Bearbeitungszeit, inklusive Queue-
  Wartezeit, Decode und Emission.
- `worker_reserve_min_us`: kleinster Rest bis zum **auf Callback-Beginn
  abgerundeten Pull-Deadline**, nicht bis zum einzelnen Sample. Die Ausgabe-
  Queue wird nur einmal pro Callback übernommen. Bei Quantum 1024 steht in
  diesem Graphen ein Callback-Zeitraum zur Verfügung, nicht zwei.
- `input_queue_peak_frames`, `late_frames`, `underrun_frames`,
  `queued_muted_frames`, `transport_faults`: Backlog und getrennte Verlust-/
  Schutz-/Transportzähler. Anlauf vor dem ersten verzögerten Eingang zählt
  nicht als Underrun.

Extrema und Zähler gelten über die Lebensdauer des Helfers. Die Reserve ist
eine gemessene Scheduling-Schätzung bei aktuellem Quantum; kein Nachweis gegen
beliebig lange OS-Stalls. `/api/audio/surround` reicht diese Werte weiter. AV
Sync und Demo verwenden **3074/48 ms** statt 8192/48 ms, weiterhin codec-
unabhängig und nur bei tatsächlich geroutetem Helfer. Vorhandene Align-Werte
und Zielwerte werden nicht umgeschrieben.

### Private Live-Tests auf `.104`

`SURROUND_STRESS=1 python3 native_surround/test_live.py` bestand alle zehn
Fälle. Die Schnellwechsel enthalten PCM→AC-3 stereo→DTS 5.1→AC-3 5.1→PCM,
96/64/96-ms-Codecsegmente, inklusive des sicheren PCM-Fallbacks. Sechs Busy-
Worker auf vier Kernen liefen während des Lastfalls. Alle zehn Outputs wurden
gegen die Offline-Ausgabe desselben aufgezeichneten Rohträgers verglichen,
inklusive erwarteter Stille, ohne stille Aussetzer aus dem Vergleich zu nehmen.

| Quantum 1024, je 8 s | Worker max. | Kleinste Reserve | Peak Inputqueue | Verluste/Fehler |
|---|---:|---:|---:|---:|
| Schnellwechsel, ohne Zusatzlast | 1,972 ms | **19,360 ms** | 1024 Frames | 0 late / 0 underrun / 0 faults / 0 errors |
| Schnellwechsel, sechs Busy-Worker | 3,216 ms | **18,116 ms** | 1024 Frames | 0 / 0 / 0 / 0 |

Die Runs prüften 359424 bzw. 358400 Carrierframes über alle zehn Outputs.
Bei den sechs Einzelcodec-/Tap-Fällen mit Quantum 256 betrug der Offset
**3330 = 3074 +256**; ebenfalls keine Late-/Underrun-Frames. Die größte
gemessene Worker-Zeit über diese Fälle war 3,105 ms; die kleinste Reserve
über alle Einzelcodec-/Tap-Fälle betrug 18,300 ms.
Rate 44100 und Quantum 2048 wurden separat als stumme Schutzfälle geprüft.

Der Nicht-RT-Beobachter kann blockweise vor/nach dem Helfer laufen; deshalb
werden ganze Blöcke bei 3074 bzw. 4098 Frames beobachtet. Die unabhängig
gemeldeten Helper-Verlustzähler müssen trotzdem null bleiben. Stille Blöcke
sind kein Latenzbeleg; die konstanten PCM-/Codec-Testfälle sichern die
Frame-Zeitachse zusätzlich ab. Reproduzierbare Artefakte liegen auf `.104`
unter `/tmp/opencode/spdif-opt-private-ready/` (Carrier, Aufzeichnung,
`metrics.json` mit Statusverlauf).

Zusätzlich bestanden 26 native Ingress-Regressionsfälle und der C-Queue-Test:
maximaler korrupter DTS-Payload mit rechts beginnender Präambel, PCM-Tail,
6144-Frame-E-AC-3-Perioden, 36 Ein-Burst-Codecwechsel, Queue-Wrap, zukünftige
Recovery oberhalb des Watermarks, Reset und Late-Discard. Neu geprüft wurden
Startup mitten im AC-3/DTS-Payload auf diskreten/stereo Outputs, Roh-DTS-
Perioden 7000/8192, Wechsel von Abstand/Packing nach Bestätigung und deren
Recovery zu IEC mit kurzem PCM-Fallback. Die
Live-Codecquellen haben keinen vorgeschalteten Stille-Präfix mehr. Der Queue-Test
bestätigt die Unterdrückung von 351 bereits emittierten Roh-DTS-Frames, die
das verkürzte Pending-Fenster allein nicht mehr erreichen würde. Die fünf
Prüfungen historischer realer TV-Captures wurden mangels Dateien explizit
übersprungen; sie werden nicht als neue Messbelege gezählt.

### Deploy und Live-Verifikation im Produktgraphen

Die geprüfte Fassung wurde lokal committed und auf `.104` installiert. Das
Produktbinary ist byte-identisch zum zuvor geprüften Staging-Binary. Service
und HTTP sind aktiv; die ausgelieferten AV-Sync-/App-/CSS-Assets wurden gegen
ihre Dateien geprüft. Die Browseranzeige meldet **Input: 64,04 ms**, bei
bestehendem 5-ms-Limiter **69,04 ms Minimum**. Kein Push oder Release.

Der reale TV lieferte diesmal aktives PCM (Probe: Peak 0,121368, nicht die
digitale Stille der alten Analyse). Deshalb wurde **kein** Carrier in den
Produktgraphen injiziert. Stattdessen zeichnete ein passiver 12-Kanal-
Recorder den bestehenden Rohträger und alle zehn Helferoutputs gemeinsam
auf. Auswahl, Clock und Produktlinks blieben unverändert. Sechs Busy-Worker
liefen nur im Lastfall; kontrollierte Formatwechsel sind separat im privaten
Graphen auf derselben Maschine belegt, nicht am aktiven TV nachgestellt.

| Produktgraph, je 30 s | Verglichene aktive Frames | Größte Sampleabweichung | Counter-Deltas |
|---|---:|---:|---:|
| Ohne Zusatzlast | 1420288 | 6,71e−8 | 0 late / 0 underrun / 0 faults / 0 errors |
| Sechs Busy-Worker | 1418240 | 4,47e−8 | 0 / 0 / 0 / 0 |

Worker-Extrema über dieselbe Helferlebensdauer: **max. 5,065 ms Bearbeitungszeit,
min. 16,268 ms Reserve**, maximal 1024 Frames Inputbacklog. Der Lastfall
verschlechterte diese bereits im Leerlauf beobachteten Extrema nicht; die
Werte sind kumulativ, keine voneinander unabhängigen Per-Run-Maxima.
Die Referenz ist die Offline-Dekodierung des tatsächlich aufgezeichneten
TV-Trägers. Alle verglichenen Frames waren aktiv; fehlende Audioframes wurden
nicht als erlaubte Stille übersprungen.

Der Beobachter sah im Leerlauf konstant **3074 Frames**, unter Last ganze
Blöcke bei 3074 oder **4098 = 3074 +1024**. Das bestätigt den bekannten
Nicht-RT-Beobachterhop, ohne Verluste im Helfer: dessen Late-/Underrun-Zähler
blieben unverändert. Messartefakte auf `.104`:
`/tmp/opencode/spdif-opt-product-idle/` und
`/tmp/opencode/spdif-opt-product-loaded/` (`record.f32`, `input.s32`, diskrete
und Stereo-Referenz, `metrics.json`, `result.json`). Der passive Prüfer liegt
unter `/tmp/opencode/verify_spdif_production.py`. Testprozesse und ihre
temporären Recorderlinks wurden beendet; die verifizierte Produktfassung
bleibt installiert.

Die TV-/HDMI-Passthrough-Latenz und physische Gesamt-AV-Latenz bleiben wie in
Abschnitt 5 ungemessen. Die sicher belegten Werte beziehen sich auf Helfer,
Queue-Zeitachse und beobachteten Graph-Transport.
