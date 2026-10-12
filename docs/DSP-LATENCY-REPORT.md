# DSP-Latenzen in FXRoute — Bestandsaufnahme für ein zentrales AV-Sync-Management

Die Abschnitte 1–14 dokumentieren die Bestandsaufnahme vor AV Sync und das
daraus abgeleitete Konzept. Abschnitt 15 beschreibt die Implementierung.
Alle Werte sind aus dem Code abgeleitet oder auf der Testmaschine gemessen;
offene Messgrenzen sind ausdrücklich gekennzeichnet. Korrektur vom 08.10.2026:
PCM durch den Live-Surround-Helfer hatte ebenfalls 8192 Frames Eingangslatenz;
die frühere Nullmessung betraf nur dessen Offline-Decoder, nicht den Ingress.

Optimierung vom 09.10.2026: Der aktuelle Helfer nutzt für PCM/AC-3/DTS
**3074 Frames (64,04 ms)** mit 2050 Frames Erkennungsfenster und 1024 Frames
Worker-Marge. Aktuelle Werte unten sind entsprechend nachgeführt; die alte
Umschaltmessung in 6.4 bleibt als historische Vergleichsbasis erhalten.
Messbelege und Schutzgrenzen: `docs/SPDIF-LATENCY-ANALYSIS.md`, Abschnitt 10.

Korrektur vom 10.10.2026: Helfer jetzt **2818 Frames (58,71 ms)** mit 2050
Frames Lookahead und **768 Frames (16,00 ms)** Marge, Quantum **768** (erzwungen,
bei Stopp restauriert). 3071/1024 wurde wegen systematischer
Late/Underrun-Verluste verworfen, 256 wegen DSP-Instabilität. Historische
3074er-Werte unten bleiben für die alte Fassung gültig; Produkt-Livebelege für
2818/768 folgen nach Deploy und TCL-Prüfung.

## 1. Vorgehen und Belege

- Codeanalyse aller Stufen im Native-DSP-Engine (`native_dsp/`), des
  Stufen-Compilers (`dsp/manager.py`), der Live-Update-Erkennung
  (`dsp/runtime.py`), des Sample-Rate-Pfads (`native_src/`) und des
  Surround-Eingangs (`native_surround/`).
- Messungen auf der Testmaschine mit dem *unveränderten* Worktree-Stand:
  - Impulslatenz jeder Stufe über die Offline-Engine
    `native_dsp/build/fxroute-dsp-offline` (Impuls bei Frame 0, Peak-Position =
    Latenz). Gebaut mit `sh native_dsp/build.sh`.
  - LV2-Ports über `native_dsp/test_lv2_host.sh` und eigene `/tmp`-Messprogramme
    (`native_dsp/lv2_host.c` als Host-Wrapper).
  - **Zwei unabhängige Latenzschätzer neben dem Impulspeak**, weil der Impulspeak
    bei einigen Plugins nicht die Gruppenlaufzeit trifft (siehe 4.2):
    Kreuzkorrelation mit Rauschen und **Least-Squares-Lag-Schätzer**
    (`argmin_lag Σ (y[k+lag] − x[k])²`) über ein 80…160k-Sample-Rauschfenster.
  - Reale Produktketten über `DSPManager.compile_engine_text` + Offline-Engine
    (System-Python des App-Venv, wegen `numpy`).
  - Surround-Decoder: echte AC-3-/DTS-Bitstreams (ffmpeg 8.1.2, 48 kHz) im
    IEC-61937-Träger (ffmpeg-`spdif`-Muxer) durch
    `native_surround/build/fxroute-surround-input --offline` und Vergleich
    gegen die ffmpeg-Referenzdekodierung.
  - Live-Graph: `pw-metadata -n settings`, `pw-dump`.
- Reproduzierbare Befehle im Anhang (Abschnitt 14). Auf der Maschine wurde
  nichts deployt; es liefen nur Build- und Messartefakte in `native_dsp/build`,
  `native_surround/build` und `/tmp`.

## 2. Kurzfassung der Befunde

1. Kein Produktpfad meldet heute eine Latenz. `fx_lv2_host_latency`,
   `fx_crystalizer_latency`, `fx_upmix_fast_latency`, `fx_upmix_hq_latency`
   existieren, werden aber **nur in Tests** verwendet.
2. Der LV2-Latenz-Port ist nur **teilweise** verlässlich: für EQ (6144/4096),
   Limiter und Maximizer korrekt, für die Loudness (`loud_comp_stereo`)
   **unbrauchbar** (konstant/stale 7680, siehe 4.3).
3. Die Latenzen der Stufen sind **seriell additiv**. Gemessen:
   Limiter (5 ms) + Loudness (4096) + Crystalizer (1904) = **6240 Samples
   = 130,0 ms** bei 48 kHz. Der `Direct`-Preset (`bypass 1`) liefert **0**.
4. In Surround sind die Pfade **pro Gruppe und pro Output unterschiedlich lang**
   (gruppen-gebundene Plugins + Output-Bank), gemessen: FL/FR 2048, FC 1024,
   SL/SR/BL/BR/LFE 0 Samples.
5. **Loudness-Latenz = gewählte FFT-Größe** in Samples (256…16384), dreifach
   unabhängig bestätigt; der Plug-in-Port meldet dagegen dauerhaft 7680 und hinkt
   einer Laufzeit-Änderung sogar um eine Änderung nach (4.3).
6. **EQ-Latenz: IIR 0, FIR/FFT 6144, SPM 4096** Samples — bandzahl-, raten- und
   `react`-unabhängig; der Port stimmt. Die früher gemessenen 10240 waren ein
   Artefakt (leerer Ausgang während des Plugin-Warm-ups, 4.2).
7. **Crystalizer: Speex-Anteil 80 Samples bei allen acht getesteten Raten**
   (44,1…384 kHz); die Gesamtlatenz folgt der Codeformel (4.4).
8. **Surround-Eingang:** PCM und komprimierte Streams (AC-3/DTS) durch den
    Live-Helfer sind um **3074 Frames (64,04 ms)** verzögert. Direkte PCM-Pfade
   außerhalb dieses Helfers haben diesen Anteil nicht. Ein Wechsel
    Bitstream→PCM wartet sicher bis zur Sync-Abwesenheit von mehr als 6656 Frames;
    nach AC-3 bleiben **5122 Frames ≈ 106,71 ms**, nach DTS-512 **6146 Frames ≈ 128,04 ms**.
    PCM→Bitstream erhält den vollständigen etablierten PCM-Tail (6.4).
9. **Live-Änderungen können die Latenz mitten im Stream verschieben**, ohne
   Rebuild: gemessen Loudness `fft` 256 → 8192 (Δ 7936 Frames = 165,3 ms) direkt
   hintereinander (8.2). Ein Latenz-Balance, die nur den Compile-Zustand kennt,
   ist daher nicht ausreichend.
10. Der `effects bypass` (Preset `Direct`) verwirft die gesamte Stufenkette und
    ist **kein** Compile-Vorgang — heute sieht das keine Latenzbilanz.
11. **Die vorhandene Time Alignment ist Referenz, nicht Fehler.** Die
    Ankunftsmessung (Speaker Align, AutoSub) misst die Pfadlatenz *mit*, die
    gespeicherten Align-Werte können die algorithmische Spreizung also bereits
    enthalten. Ein Ausgleich darf deshalb nur die **Änderung** der
    algorithmischen Latenz gegenüber dem Kalibrierzeitpunkt egalisieren
    (`D_i = max_j(L_j−L^k_j) − (L_i−L^k_i)`), nie die Summe `A_i + L_i` — sonst
    wird die Korrektur am Hörplatz zerstört oder verdoppelt (12.10.3/12.10.4).
12. **Der Ausgleich braucht einen eigenen Delay-Slot:** Das Align-Fenster ist
    ±40 ms (`audio/output_state.py:192`), die Rebase-Regel verschiebt alle
    Ausgänge (`measurement/speaker_align.py:405-441`), und die Commit-Validierung
    lässt nur `alignment_ms`/`level_db` zu
    (`measurement/speaker_commit.py:79-130`) — ein einzelner FIR-EQ füllt die
    128 ms Spreizung allein (12.10.5).
13. **AV Sync ist eine Wunsch-Gesamtverzögerung, kein Zusatz.** Die Bilanz
    rechnet die vorhandene Kette (Eingangszweig + DSP-Stufen + Alignment) als
    **Guthaben** an: `C = max(0, W − T_ref)`, Untergrenze `W_min = T_ref`
    (12.2/12.4). Die Ausgleichsformel wurde von der absoluten Spreizung
    `L_max − L_i` auf die Änderung seit der Kalibrierung `maxΔ − Δ_i` umgestellt
    (12.11).

## 3. Signalkette (Ist-Zustand)

Reihenfolge, wie `dsp/manager.py::compile_engine_config` sie erzeugt
(`chain.insert(0, upmixer)`; Extras werden an die Preset-Kette angehängt,
`_extras_chain`, `manager.py:1259-1276`):

```
Quelle → [PipeWire-Graph / SRC-Adapter] →
  Upmixer (nur Surround, optional)
  → Preset-Kette (PEQ/EQ (LV2), Convolver (nativ), Delay-Bänder (nativ))
  → Headroom → Delay → Tone (Crystalizer | Maximizer)
  → Bass Enhancer → Autogain → Loudness → (global-loudness-master)
  → Protection Limiter
  → Output-Matrix  (matrix / postmatrix / gmatrix)
  → pro Output: Crossover-SOS (xsos/gxsos) → Output-PEQ (sos/peq)
               → Output-Convolver (oconv) → Output-Delay + Gain + Polarität
  → Hardware-Sink
```

Wichtig für die Latenzbilanz:

- Der **Upmixer** läuft als einzige Stufe auf dem 8-Positionen-Surround-Bus
  (`fxdsp_process_tapped`, `dsp.c:1211-1285`) und verzögert **alle** Kanäle
  gleich. Seine Latenz gilt nur für ein Stereo-Programm: natives
  Mehrkanalmaterial umgeht ihn ohne Verzögerung (Nachtrag in 4).
- **Convolver, Crystalizer, Autogain und LV2** laufen pro Kanal bzw. pro
  Effekt-Paar (`run_jobs`, `dsp.c:899,1231,1243`). In Surround existiert ein
  `pair_mask` pro Stufe (`pairs`, `dsp.c:87,653`), d. h. ein gruppen-gebundenes
  Plugin verlängert **nur** die Pfade seiner Paare (FL/FR=0, FC=1, LFE=2,
  SL/SR=3, BL/BR=4, `pair_left/pair_right`, `dsp.c:158-177`; Compiler-Seite
  `pairs {GROUP_PAIR_MASK[...]}`, `manager.py:1121`).
- Die **Output-Bank** (Crossover, Output-PEQ, Output-Convolver, Output-Delay)
  liegt hinter der Matrix und ist damit **pro Output** unterschiedlich lang.
- `bypass 1` (Preset `Direct`) überspringt die gesamte Stufenkette
  (`dsp.c:1214-1226,1282`); die Output-Bank läuft weiter.
- Die **Delay-Stufen** sind zweikanalig (`param left_ms`/`param right_ms`,
  `manager.py:1162-1163,1249-1250`) — eine Delay-Stufe kann also selbst einen
  L/R-Versatz erzeugen.

Damit ist die Gesamtlatenz **nicht** ein Skalar, sondern ein Vektor pro
Ausgang: `L_output = L_upmixer + L_stufen(paare dieses Outputs) +
L_outputbank(ausgang)`.

## 4. Gemessene Latenzen pro Komponente

Alle Werte sind Impuls-Peak-Abstände (Frames) aus der Offline-Engine, jeweils
mit 1024er Quantum; die mit **✓** markierten Zeilen wurden zusätzlich mit
Least-Squares-/Kreuzkorrelations-Lag bestätigt (siehe 4.2/4.3).
`rate` ist die Stream-Rate.

| Komponente | Konfiguration | @48 kHz | @44,1 kHz | Charakter |
|---|---|---:|---:|---|
| Upmixer **Fast** | Stereo-Programm | **64** (1,333 ms) | **58** (1,315 ms) | ratenabhängig; Mehrkanal: 0 |
| Upmixer **HQ** | FFT 1024, Stereo-Programm | **1023** (21,31 ms) | 1023 (23,20 ms) | konfigurationsabhängig; Mehrkanal: 0 |
| Upmixer **HQ** | FFT 2048, Stereo-Programm | **2047** (42,65 ms) | 2047 (46,42 ms) | konfigurationsabhängig; Mehrkanal: 0 |
| Upmixer **HQ** | FFT 4096, Stereo-Programm | **4095** (85,31 ms) | 4095 (92,86 ms) | konfigurationsabhängig; Mehrkanal: 0 |
| Upmixer **HQ** | FFT 8192, Stereo-Programm | **8191** (170,65 ms) | 8191 (185,74 ms) | AV-Sync-Erweiterung; nativer Impulstest; Mehrkanal: 0 |
| **Loudness** (LSP `loud_comp_stereo`) ✓ | `fftSize` = S | **S** (256…16384) | **S** | konfigurationsabhängig; Default 4096 → 85,33 / 92,88 ms |
| **Protection Limiter** (LSP `sc_limiter_stereo`) | Lookahead L ms | **floor(L·rate/1000)** (5 ms → 240) | 5 ms → 220 | Port korrekt |
| **Crystalizer** (nativ) ✓ | immer | **1904** (39,667 ms) | **1839** (41,70 ms) | fest pro Rate (Formel 4.4) |
| **Maximizer** (ZaMaximX2) | immer | **480** (10,0 ms) | **480** (10,88 ms) | fest (Frames, nicht ms) |
| **Bass Enhancer** (Calf) | immer | **0** | **0** | keine |
| **PEQ / EQ** (LSP `para_equalizer_x32_lr`) | Modus IIR (**Default**) | **0** | **0** | keine |
| — derselbe EQ ✓ | Modus SPM | **4096** (85,33 ms) | 4096 (92,88 ms) | Port korrekt |
| — derselbe EQ ✓ | Modus FIR / FFT | **6144** (128,0 ms) | 6144 (139,32 ms) | Port korrekt, bandzahlunabhängig |
| **Convolver** (nativ) | Engine-Anteil | **0 zusätzlich** | 0 | siehe Abschnitt 5 |
| Linearphasiger FIR (Measurement-Convolver) | N Taps | **N/2** | N/2 | konfigurationsabhängig (Abschnitt 5) |
| **Delay**-Stufe (nativ) | X ms | llround(X·rate/1000) | dito | ≤ 500 ms (≤ 10 000 ms bei Id `…-delay`), L/R getrennt |
| **Output-Delay** (Align) | X ms | llround(X·rate/1000) | dito | ≤ 500 ms, pro Output |
| Headroom / Master-Gain | — | **0** | 0 | nur Gain |
| **Autogain** (nativ) | — | **0** | 0 | nur Gain |
| **Crossover** (Butterworth/LR-SOS) | — | **0** fest | 0 | nur frequenzabhängige Gruppenlaufzeit |
| **Surround-Eingang, komprimiert** (AC-3/DTS) | Live-Pfad | **3074** (64,04 ms) | — (nur 48 kHz) | siehe Abschnitt 6 |
| **Surround-Eingang, PCM** | Live-Pfad durch Helfer | **3074** (64,04 ms) | — | nur 48 kHz; direkter PCM-Pfad ohne Helfer: 0 |
| PipeWire **SRC-Adapter** | Rate ≠ Graph-Rate | deklariert **750 ms** | dito | siehe Abschnitt 9 |

### 4.1 Formeln, die sich direkt aus dem Code berechnen lassen

- Fast-Upmixer (`upmix_fast.c:98-103,128`):
  `half = 2·round(32·rate/48000)`; Latenz = `half`.
- HQ-Upmixer (`upmix_hq.c:227`): Latenz = `fft_size − 1`.
- **Nachtrag (Upmixer-Bypass):** Beide Upmixer-Latenzen gelten nur, solange
  die Stufe ein Stereo-Programm verarbeitet. Solange die gemeinsame
  Mehrkanalerkennung hält (Signal in FC/LFE/SL/SR/BL/BR, danach 10 s Hold),
  nimmt das Signal den direkten Pfad (`fx_upmix_path` in `upmix_common.h`):
  Eingang = Ausgang, 0 Frames, keine Matrix/keine FFT. Der Wechsel wird
  überblendet (30 ms, höchstens die Verzögerung selbst); das Programm springt
  dabei um die Upmixer-Latenz vor (Stereo → Mehrkanal) bzw. wiederholt sie
  (zurück). `fx_upmix_*_latency` und damit `stage_latency` melden den jeweils
  gewählten Pfad; der Readback führt diesen programmabhängigen Anteil je
  Zeile zusätzlich als `adaptive`, und `latency_signature` klammert ihn aus,
  weil er alle Kanäle gleich trifft und kein Wechsel des Renderings ist.
- Loudness: Latenz = die gewählte FFT-Größe in Samples
  (Mapping `fftSize → fft 0…6` im `control("fft", …)`, `manager.py:1195`). Der
  Plug-in-Port ist **nicht** zu verwenden (4.3).
- EQ: Modus-abhängig 0 / 6144 / 6144 / 4096 Samples, ratenunabhängig in Frames
  (`control("mode", eq_modes[…])`, `manager.py:1128`; `IIR:0, FIR:1, FFT:2,
  SPM:3`). Produkt-Default ist **IIR** → 0.
- Crystalizer (`crystalizer.c:414-419,580-584`):
  `kernel_size = 2·ceil(ceil(4/(120/(2·rate)))/2)+1`
  Latenz = `1024 + (kernel_size−1)/4 + speex_upsampler_output_latency/2
  + speex_downsampler_output_latency`.
  Der Speex-Anteil ist **nur zur Laufzeit** aus dem Resampler abfragbar; er wurde
  bei allen getesteten Raten zu **80** gemessen (4.4).
- Delay/Output-Delay (`dsp.c:550-556,701,728,1000`): `llround(ms·rate/1000)`,
  gedeckelt auf 500 ms Puffer (Ausnahme: Id endet auf `-delay` → 10 000 ms;
  `STAGE_DELAY_MAX_MS`/`PEQ_STAGE_DELAY_MAX_MS`/`OUTPUT_DELAY_MAX_MS`,
  `dsp.c:44-46`).
- Limiter: Latenz = Lookahead in Samples, von LSP abgerundet; der LV2-Port
  bestätigt das (0,1/1/4/16 ms → 4/48/192/768 @48 kHz, 4/44/176/705 @44,1 kHz).
  Die nativen Delay-Stufen runden dagegen per `llround`.
- Surround-Eingang durch Helfer (PCM und komprimiert): 3074 Frames = `SURROUND_LIVE_LATENCY`
  (`surround.h`, `ingress.c:process`). Das Erkennungsfenster
  `SURROUND_LOOKAHEAD = 2050` (`surround.h`, `surround.c:surround_push`) ist die
  Vorlaufzeit der Decoder-Entscheidung, **kein** zusätzlicher Summand (6.3).

### 4.2 EQ im FIR/FFT-Modus: Auflösung der 6144/10240-Abweichung

Die erste Messrunde hatte für den FIR/FFT-Modus 10240 Frames gemessen und dem
Port-Wert 6144 widersprochen. Die Auflösung: **der Port hat recht, 6144.**

Drei Schätzer, dieselbe Konfiguration, 48 kHz, Block 1024 (`/tmp/fxeqsse`,
`/tmp/fxeqoff`, `/tmp/fxxcorr`):

| Modus | Impulspeak: Frame 0 / nach Warm-up | Least-Squares-Lag | LV2-Port | LS-Residuum |
|---|---:|---:|---:|---:|
| IIR (0) | 0 / 0 | **0** | 0 | 0 (exakt transparent) |
| FIR (1) | 10240 / 6144 * | **6144** | 6144 | 6,4e-13 bei Signal 4,1e-2 |
| FFT (2) | 10240 / 6144 * | **6144** | 6144 | 6,6e-13 bei Signal 4,2e-2 |
| SPM (3) | 4096 / 4096 | **4096** | 4096 | 1,0e-13 bei Signal 4,2e-2 |

\* Der Impulspeak ist **konfigurations- und zeitpunktabhängig** und deshalb als
Latenzmaß unbrauchbar:

- Impuls bei Frame 0 (also im ersten Block direkt nach `activate`): Peak bei
  **10240**, aber die Ausgangsenergie ist praktisch null
  (Σ y² = 4,5e-16 vor 10240, 8,0e-12 danach) — das Plugin gibt im ersten Block
  noch nichts aus, der „Peak" ist ein Denormal-Rest.
- Impuls bei Frame 9000 oder 20480 (nach dem Warm-up): Peak exakt
  **6144** Frames später, Energie vor 10240 **exakt 0**.
- Ein Bell +12 dB @200 Hz verschiebt den Impulspeak um weitere ~113 Frames
  (6257 statt 6144) — die Hüllkurve des Ringens liegt hinter der
  Gruppenlaufzeit.

Belege gegen Abhängigkeiten (Noise-LS bzw. Kreuzkorrelation, `.104`): Ergebnis
6144/4096 **unverändert** für 0/1/8/16/32 Bänder je Seite, für 44,1/48/96 kHz
und für `react` 0…3. Der Wert ist also eine feste Plugin-Konstante
(6144 = 6·1024), keine Funktion der Konfiguration. Für AV-Sync zählt damit:
**jeder EQ im FIR- oder FFT-Modus kostet 128,0 ms (48 kHz) bzw. 139,3 ms
(44,1 kHz)** auf den Pfaden seiner Paare; der Port ist als Quelle brauchbar.

### 4.3 Loudness: FFT-Größe, drei Methoden und ein unbrauchbarer Port

Gemessen mit drei Methoden auf `.104` (`/tmp/fxmeasure2.py`, `/tmp/fxloudsse`,
`/tmp/fxxcorr`), jeweils 48 und 44,1 kHz:

| `fftSize` | 256 | 512 | 1024 | 2048 | 4096 | 8192 | 16384 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Impulspeak (Frames) | 256 | 512 | 1024 | 2048 | 4096 | 8192 | **16384** |
| LS-/xcorr-Lag | 256 | 512 | 1024 | 2048 | 4096 | 8192 | außerhalb 0…16383 |
| ms @48 kHz | 5,33 | 10,67 | 21,33 | 42,67 | 85,33 | 170,67 | 341,33 |
| ms @44,1 kHz | 5,81 | 11,61 | 23,22 | 46,44 | 92,88 | 185,76 | 371,52 |
| LV2-Port (alle) | 7680 | 7680 | 7680 | 7680 | 7680 | 7680 | 7680 |

- Der LS-Schätzer ist für 256…8192 **bitgenau**: Residuum 1,5…1,9e-11 bei
  Signalleistung 1,7e-3, d. h. der Prozessor verhält sich wie eine reine
  Verzögerung um die FFT-Größe. Für 16384 findet sich **kein** Lag im
  Sweep-Fenster 0…16383 (Residuum ≈ Signalleistung), was genau zur
  Impulsmessung 16384 passt.
- Frühere Auffälligkeiten sind damit erklärt: die `xcorr`-Werte 11889 (fft 6)
  und 10240 sind Artefakte eines Fensters ohne Korrelation bzw. eines leeren
  Ausgangs; belastbar ist **Latenz = fftSize**.
- Der Port ist konstant 7680 und **hinkt der Realität hinterher**: Wird `fft`
  zur Laufzeit geändert, meldet der Port anschließend 7680 und danach den
  *vorherigen* Wert (nach dem Wechsel 2→4 in der Steady-State-Messung 4096
  gemeldet bei tatsächlich 1024 usw.). Der Port ist für die Loudness als
  Latenzquelle **nicht verwendbar**.

**Live-Verhalten (siehe auch 8.2):** Das Plugin übernimmt einen `fft`-Wechsel
zur Laufzeit. Gemessen über denselben Host-Wrapper, den die Engine benutzt:
1024 → 4096 (Δ +3072 Frames) und 256 → 8192 (Δ +7936 Frames = 165,3 ms) mitten
im Stream, jeweils ohne `activate`. Damit ist die Loudness-FFT-Größe der größte
Latenzhebel im Produkt, und sie ist als **Value** klassifiziert (8.2).

Konsequenz für die bestehende Annahme in `dsp/manager.py:412-425` /
`dsp.c:600-620` („matched-latency loudness compensation", `lv2_frame_size =
2^(rank−1)`): Das Crossfade-Timing ist damit konsistent, die **Latenz** ist aber
exakt die FFT-Größe und nicht der Port-Wert 7680. Die Annahme sollte darauf
geprüft werden.

### 4.4 Crystalizer über acht Sampleraten

Impulsmessung, `.104`, Offline-Engine (`/tmp/fxcry.py`):

| Rate | 44100 | 48000 | 88200 | 96000 | 176400 | 192000 | 352800 | 384000 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| gemessen | 1839 | 1904 | 2574 | 2704 | 4044 | 4304 | 6984 | 7504 |
| `(kernel−1)/4` | 735 | 800 | 1470 | 1600 | 2940 | 3200 | 5880 | 6400 |
| 1024 + Kern + Speex | 1839 | 1904 | 2574 | 2704 | 4044 | 4304 | 6984 | 7504 |

- Der **Speex-Anteil ist bei allen acht Raten 80 Samples**; die Gesamtlatenz
  folgt der Codeformel exakt (Differenz 0 in allen acht Fällen).
- Der Anteil bleibt auch dort 80, wo der Kern-Term mit der Rate wächst, ist also
  in dieser Implementierung praktisch ratenunabhängig (die Resampler-Ratio ist
  fest). Eine Rate, bei der er abweicht, wurde nicht gefunden; die Laufzeit-
  Abfrage bleibt trotzdem nötig, weil der Wert nicht aus der Rate ableitbar ist.

### 4.5 Was weiterhin nur gemessen werden kann

- **Speex-Latenzanteil** im Crystalizer
  (`speex_resampler_get_output_latency`, `crystalizer.c:583-584`): laufzeit-
  abhängig; hier konstant 80.
- **LSP-FIR-Warm-up**: Der erste Block nach `activate` liefert keinen Ausgang
  (4.2). Die Größe dieses Effekts ist für den EQ gemessen (Peak bei 10240,
  Amplitude ~1e-12), aber nicht generell für alle LSP-Plugins bestimmt.
- **Loudness-Port-Ursache**: Warum `out_latency` 7680 meldet und um eine
  Änderung nachhinkt, ist aus dem TTL nicht erklärbar; gemessen ist es.
- Die Werte der physischen Kette (DAC/AVR/TV) — siehe Abschnitt 13.

## 5. Convolver: Partitionierung und Filterlänge

`native_dsp/dsp.c:294-380`:

- `CONV_BLOCK = 256`. Der erste 256-Tap-Block läuft als **direkter FIR-Kopf**
  (`head_fwd`, zwei Kopien im Ringpuffer) und liefert **0 Samples zusätzliche
  Latenz**.
- Alle weiteren Taps laufen als **uniform partitionierter Tail** mit
  512-Bin-FFTs (`partition_count = (count−1)/256`, `dsp.c:300`), ausgerichtet
  über `tail_output[input_pos]` — die Blocklatenz ist durch die Struktur
  kompensiert, es entsteht **kein weiterer Offset**.
- Messung: 1-Tap-IR → 0; 256-Tap-IR, Tap bei 0 → 0; 4096-Tap-IR, Tap bei 2048 →
  2048. Die Engine trägt also nur die **im IR selbst liegende Verzögerung** bei.
- IRs werden in `load_wav` (`dsp.c:216-282`) per `libsamplerate` (SINC_BEST) auf
  die Stream-Rate umgerechnet und mit `/ratio` normiert. Die Dauer bleibt
  erhalten, die Sample-Latenz skaliert mit der Stream-Rate.
- Der IR-Pfad ist ein **Shape-Merkmal**: `param path` (`manager.py:1157`) →
  jede IR-Änderung baut die Engine neu (8.2).

Konsequenz für die AV-Latenz: Der **Measurement-Convolver** erzeugt je nach
`phaseMode` (`static/measurement_dsp.js:180-345`) einen IR mit eingebauter
Verzögerung:

- `linear` (Default): Spektrum wird um `half = length/2` zirkulär verschoben
  (`measurement_dsp.js:207-213`) → **N/2 Samples** Gruppenlaufzeit
  (4096 Taps → 2048 = 42,67 ms @48 kHz; 8192 → 4096 = 85,33 ms). Auf der
  Maschine bestätigt (2048 bzw. 4096).
- `minimum`: energie-vorne, ~0 zusätzliche Latenz.
- `hybrid_aligned`: dieselbe `N/2`-Rampe, mit Minimum-Phase im Bassanteil
  gemischt.
- `minimum_aligned`: Minimum-Phase plus bewusster L/R-Zeitversatz.

Das ist eine **konfigurationsabhängige Latenz, die der Engine-Anteil nicht
erklärt** und die heute nirgends gemeldet wird.

## 6. Surround-Eingang: AC-3/DTS (gemessen)

Aufbau: `native_surround/` erkennt IEC-61937-Bursts im S/PDIF-Träger, dekodiert
mit persistenten FFmpeg-Kontexten (AC-3/DTS Core, E-AC-3), projiziert die
Layouts auf den 8-Kanal-Bus und puffert für die Erkennung. Der Offline-Modus
(`--offline`) entfernt die Erkennungsverzögerung; der Live-Pfad nicht. Gemessen
mit `ffmpeg`-Fixtures (48 kHz, 1–2 s Sinus-Mix) über
`native_surround/build/fxroute-surround-input --offline` (`/tmp/fxdec2.py`,
`/tmp/fxdec3.py`).

### 6.1 Burst-Geometrie und 1:1-Abbildung

| Stream | Trägerframes | dekodierte Frames | Bursts | Frames je Burst | Verhältnis |
|---|---:|---:|---:|---:|---:|
| AC-3 stereo, 2 s | 96768 | 96768 | 63 | **1536 (32,0 ms)** | 1,0000 |
| AC-3 5.1(side), 2 s | 96768 | 96768 | 63 | 1536 | 1,0000 |
| DTS 5.1(side), 2 s | 96256 | 96256 | 188 | **512 (10,67 ms)** | 1,0000 |
| DTS 5.1(side), 1 s | 48128 | 48128 | 94 | 512 | 1,0000 |

- Der IEC-Träger transportiert genau **einen Stereo-Frame je dekodiertem
  Sample-Frame** (Verhältnis 1,0000 für beide Codecs) — es gibt keine
  zusätzliche Trägerstreckung.
- Die Ausgabe des Offline-Helpers ist **bit-identisch** zur
  ffmpeg-Referenzdekodierung (max. Abweichung 0,0e+00 über alle Kanäle,
  RMS 0,03521 in beiden), und die Frame-Anzahl entspricht exakt der
  Eingabe-Frame-Anzahl: Die Erkennungsverzögerung ist offline exakt
  herausgerechnet, es findet kein Resampling und kein Rematrixing statt.
- **Decoder-Innenlatenz: keine über die codierte Frame-Granularität hinaus.**
  Beide Decoder sind frame-synchron (ein Burst hinein = ein Frame heraus). Die
  praktische Untergrenze ist die Frame-Dauer: 32,0 ms bei AC-3, 10,67 ms bei
  DTS — sie ist eine Eigenschaft des Formats, keine Zusatzlatenz der
  Implementierung. Eine codec-abhängige *Gesamtverzögerung* zwischen AC-3 und
  DTS wurde **nicht** gemessen (beide 1:1, beide gleicher Live-Pfad).

### 6.2 PCM gegen Bitstream

- **PCM im Offline-Decoder:** Latenz **0**. Gemessen: reiner PCM-Träger
  30000 Frames → erster Nicht-Null-Ausgang bei Frame 0, `state=pcm`, `errors=0`.
  Das ist nicht die Latenz des Live-Ingress: `ingress.c` puffert auch diese
  PCM-Ausgabe um 3074 Frames.
- **PCM und komprimiert durch den Live-Helfer:** 3074 Frames = **64,04 ms**
  (`SURROUND_LIVE_LATENCY`, `ingress.c:process`). Der Live-Ausgang liest bei
  Eingabe-Frame `N` den dekodierten Inhalt von Index `N − 3074`; die
  Inhalt-Zeitachse wird beim Emittieren 1:1 weitergeführt
  (`surround.c:403-408`, `live_emit` mit `base + frame`), sodass Träger-Frame `k`
  um `k + 3074` am DSP-Eingang anliegt, zuzüglich Graph-Transport.

**Ergänzung vom 09.10.2026:** Die automatische Eingangsstufe wird nun auch in
Stereo und Stereo + Sub verwendet. Ihre zusätzlichen Stereo-Downmix-Ports
teilen dieselbe Inhalt-Zeitachse und denselben algorithmischen Offset wie die
acht diskreten Ports; PCM und kodiertes Stereo bleiben pegelgetreu. Der
erweiterte `native_surround/test_live.py` zeichnet im privaten PipeWire-Graphen
Rohträger und Helferausgang synchron auf und vergleicht sie sample-genau mit
der Offline-Referenz. Bei 256 Frames Quantum ergibt dieser Beobachterpfad
**3330 Frames = 3074 + 256**: Zum Helferpuffer kommt ein Graph-Scheduling-Hop
(vor der Optimierung: 8448 = 8192 +256).
Dieser Transportanteil ist nicht Teil des bisherigen algorithmischen AV-Sync-
Providers und keine Messung der physischen TV-/ADC-/DAC-Kette.

### 6.3 Erkennungsfenster

`SURROUND_LOOKAHEAD = 2050` (`surround.h`): Der Decoder emittiert einen Frame
erst, wenn 2050 weitere Trägerframes eingelesen sind
(`if (d->input > SURROUND_LOOKAHEAD)`, `surround.c:surround_push`) — er kann also
innerhalb dieses Fensters noch nachträglich stumm schalten. Damit ist der
Vorlauf **kleiner** als die Live-Verzögerung (2050 < 3074) und wird von ihr
abgedeckt: **er ist kein Summand**. Der Status meldet ihn als
`lookahead_frames=2050`. Ein atomarer Safety-Watermark erreicht zusätzlich
noch nicht gepullte Output-Queue-Frames, ohne deren Worker-Slots zu verändern.

### 6.4 Umschaltverhalten (gemessen)

Historische Messung **vor** der Optimierung:

| Übergang | Messung | Länge |
|---|---|---:|
| PCM → Bitstream | reiner PCM-Träger 20000 Frames, letzter hörbarer PCM-Frame bei **15903** → die letzten **4096** Frames werden zurückgehalten und stumm geschaltet | 4096 = 85,33 ms |
| Bitstream → PCM | AC-3-Stream (49152 Frames) + PCM: erster hörbarer PCM-Frame **10754** Frames nach dem Stream-Ende; konsistent mit `SURROUND_SYNC_TIMEOUT 12288` (`surround.h:11`) minus einen AC-3-Frame (1536) nach der letzten Präambel | 10754 ≈ 224 ms |
| AC-3 → DTS → AC-3 | ein Stream, 1 s je Abschnitt: **keine** Lücke, `errors=0`, `decoded_bursts=158` (= 32 + 94 + 32), Inhalt je Abschnitt deckungsgleich mit der Referenz | 0 |
| AC-3 stereo → AC-3 5.1(side) (Layoutwechsel) | `test_surround_ingress.py` und eigene Messung: Kanalhistorie wird per `avcodec_flush_buffers` zurückgesetzt, Audio bleibt sample-genau, `errors=0` | 0 |

Aktuell erhält PCM→Bitstream jeden etablierten PCM-Frame vor der Präambel. Ein
erster Burst innerhalb des Anlauffensters löscht weiterhin den unklassifizierten
Präfix, damit ein mitten im Payload beginnender Eingang nicht verrauscht. Direkte
Codec-/Layoutwechsel bleiben lückenlos. Der sichere Rückweg nach PCM beginnt
bei letzter Präambel +6658: nach AC-3 **5122 Frames (106,71 ms)**, nach DTS-512
**6146 Frames (128,04 ms)** nach Burst-Ende. Die **Stille-Länge** ist im
Live-Pfad dieselbe wie offline; beide Kanten werden um dieselbe feste Latenz
verschoben. Die Regel bleibt länger als die maximale E-AC-3-Periode (6144).

### 6.5 Grenzen

- **Nur 48 kHz** (`position->clock.rate == 1/48000`, `ingress.c:100-101`);
  jeder andere Graph-Rate liefert Stille (fail closed).
- Roh-DTS wird nur zur Unterdrückung erkannt, nicht dekodiert.
- Nur Quantum ≤1024: Größere Callbacks enthalten Pulls, deren Erkennung erst
  im selben Callback eingelesen würde, und liefern deshalb Stille. Der Helfer
  erzwingt keine Quantum-Änderung. Worker-Telemetrie belegt die Reserve im
  geprüften Graphen; beliebige OS-Stalls können weiterhin fail-closed Stille erzeugen.
- Die ursprüngliche Messrunde leitete den Live-Offset aus `ingress.c` und dem
  Offline-Verhalten ab. Die Ergänzung vom 09.10.2026 prüft ihn auch im privaten
  PipeWire-Testgraphen (6.2), weiterhin **nicht** physisch end-to-end.

## 7. Serielle und parallele Pfade

### 7.1 Additivität (gemessen, 48 kHz, Stereo)

- Limiter lk=5 ms → 240; Loudness 4096 → 4096; Crystalizer → 1904;
  alles zusammen → **6240 = 130,0 ms**.
- `Direct`-Preset mit denselben Extras → **0** (Bypass verwirft die gesamte
  Stufe-Kette, siehe 8.3).

### 7.2 Gruppen- und Output-Spreizung (gemessen, Surround, 8 Kanäle)

- Convolver (4096 Taps, linear) nur auf `pairs 1` (FL/FR) + Loudness
  (`fft 2` = 1024) nur auf `pairs 2` (FC) ergibt **FL/FR = 2048, FC = 1024,
  LFE/SL/SR/BL/BR = 0**.
- Der Upmixer **Fast** verschiebt alle 8 Kanäle um 64 Frames (gemeinsam).
- `output_delay_10ms_44k`: Output 0 bei 441 Frames, Output 1 bei 0 — die
  Output-Bank ist pro Ausgang eigenständig.

### 7.3 Parallele Pfade vollständig erfassen (Verfahren)

Die Gesamtlatenz ist ein Vektor über die Ausgänge. Vollständig erfassen heißt:
**jede Stufe beiträgt einen Wert und kennt ihre Reichweite.**

```
L_out(i) = L_upmixer                                  (global, alle Kanäle)
         + Σ_stufen  (pair_mask(stufe) deckt i ? L(stufe) : 0)
         + L_outputbank(i)                            (pro Output)
```

1. **Reichweite pro Stufe** ist im Compiler bereits bekannt: globale Stufen
   (Upmixer, Headroom, Master, Loudness ohne Gruppenbindung, Limiter) wirken auf
   alle Ausgänge; `pairs`-gebundene Stufen (EQ, Convolver, Crystalizer, Autogain,
    Extras in Surround) nur auf die Paare ihrer Maske (`manager.py:1121`,
   `dsp.c:653`); die Output-Bank (Crossover, Output-PEQ, Output-Convolver,
   Output-Delay) wirkt auf genau einen Ausgang (`dsp.c:1170-1210`).
2. **Wert pro Stufe:**
   - native Stufen: Formeln aus 4.1 (Crystalizer braucht den Speex-Laufzeitwert),
   - LV2-Stufen: **`out_latency` nach dem ersten verarbeiteten Block** lesen. Vor
     dem ersten `run` meldet der Port 0 (gemessen), nach dem ersten Block die
     echte Latenz (EQ: 6144). Ausnahme Loudness: Port unbrauchbar → Formel.
   - Convolver: Länge und Phasenmodus des IR (`N/2` bei linearphasigen
     Korrektur-IRs),
   - Delay/Output-Delay: aus den konfigurierten ms,
   - Eingangszweig: separat (Abschnitt 12.5).
3. **Zusammenführen:** pro Output summieren, dann das **Maximum** über die
   gerouteten Ausgänge bilden. Dieses Maximum ist der Bezugspunkt für die
   Kompensation (12.2/12.3).
4. **Nicht addieren** darf man: Upmixer-Verluste/Bypass (alles-oder-nichts),
   gruppen-gebundene Stufen auf fremden Ausgängen und die Eingangszweige
   (PCM/Bitstream/SRC sind Alternativen, keine Serienglieder).

## 8. Dynamische Latenzänderungen und ihre Erkennung

### 8.1 Was die Latenz ändert

**Ohne Latenzwirkung:** PEQ-Gain/Frequenz/Q, Crossover-Frequenz, Level,
Polarität, Headroom, Gain, Autogain-Ziel, Bass-Enhancer-Parameter,
Convolver-Dry/Wet/Gain.

**Mit Latenzwirkung:** Loudness-`fftSize`, Limiter-`lookaheadMs`, EQ-Modus,
Convolver-IR (Länge/Phasenmodus), Crystalizer an/aus, Upmixer an/aus und
FFT-Größe, Delay-Bänder und Output-Delay, `effects bypass`, sowie der komplette
Konfig-Aufbau (Preset-/A/B-Wechsel, Stufe hinzufügen/entfernen).

### 8.2 Live vs. Rebuild — die Klassifizierung des Runtimes

`dsp/runtime.py::_live_config` zerlegt die Engine-Text-Config in **Shape** und
**Values** (runtime.py:1247 ff.); `_try_live_update` (1299 ff.) schickt bei
gleicher Shape `live …`-Tupel, sonst `swap config` (1174) als atomaren
Neubau. Ergebnis:

| Änderung | Einordnung | Latenzwirkung |
|---|---|---|
| `control fft` (Loudness, `manager.py:1195`) | **Value** → live | **ja**, bis 7936 Frames (gemessen) |
| `control lk` (Limiter-Lookahead) | **Value** → live | ja (0,1…20 ms) |
| `control mode` (EQ-Modus) | **Value** → live | ja (0 ↔ 6144/4096) |
| `param mode` (Upmixer fast/hq) | **Value** → live | ja (64 ↔ fft−1) |
| `param left_ms/right_ms` (Delay-Bänder) | **Value** → live | ja |
| `live output …` (Output-Delay/Gain/Polarität) | **Value** → live | ja, pro Ausgang |
| `param fft_size` (Upmixer HQ) | **Shape** → Rebuild | ja |
| `param path` (Convolver-IR) | **Shape** → Rebuild | ja |
| `stage_begin/stage_end` (Stufe an/aus, URI) | **Shape** → Rebuild | ja |
| `bypass` (Direct-Preset, `effects bypass`) | **Shape**/Laufzeitflag | **ja, bis 130 ms+** |

Konsequenz: **Die größten Latenzsprünge sind als Live-Werte klassifiziert.** Ein
Wechsel der Loudness-FFT-Größe von 256 auf 8192 verschiebt die Latenz um 7936
Frames (165,3 ms) mitten im Stream, ohne Rebuild und ohne Beteiligung des
Transitions-Koordinators. Eine Latenzbilanz, die nur beim Compile läuft, wäre
hier still falsch.

### 8.3 Erkennung — was heute vorhanden ist und was fehlt

- **Vorhanden:** Die Shape/Value-Klassifizierung (8.2) sagt verlässlich, ob eine
  Änderung einen Rebuild auslöst (`_can_hot_update`, runtime.py:1232: gleicher
  `output_key`, Rate, Layout-Länge, Eingangszahl). Bei einem Rebuild ist der
  Compiler ohnehin beteiligt und kann die Bilanz neu berechnen.
- **Vorhanden:** Der Live-Pfad ist transaktional (`live begin` / `live commit` /
  `live abort`, Timeout `LIVE_COMMIT_TIMEOUT_NS = 250 ms`); ein neuer
  Latenzwert kann in derselben Transaktion mitgeführt werden.
- **Fehlt:** `effects bypass` (`fxdsp_set_effect_bypass`, `dsp.c` Control-Befehl
  `effects bypass`) ist **kein** Compile-Vorgang. Gemessen: Preset `Direct`
  (`bypass 1`) liefert 0 Samples, obwohl Extras konfiguriert sind, und
  `upmixers_reset` (`dsp.c:433-443`, Aufruf `:1225`) holt beim Bypass-Ende alle
  Upmixer zurück.
  Ein solcher Zustand muss aus dem **Engine-Status** gelesen werden, nicht aus
  der Config.
- **Fehlt:** Die Surround-Eingangslatenz ist im DSP-Status nicht sichtbar; sie
  liegt im Status des Eingangs-Helfers (`lookahead_frames`, `state`,
  `sample_rate`, `snapshot()` von `audio.digital_input.DigitalInput`).
- **Fehlt:** Für LV2-Stufen ist der Port erst nach dem ersten Block gültig
  (gemessen); ein einmaliges Lesen direkt nach `activate` liefert 0.
- **Fazit:** Erkennung braucht drei Quellen — Compiler-Bilanz (deterministisch),
  Status des Native-Engine (Bypass, Mute, aktive Rate), Status des Eingangs
  (PCM/komprimiert/Nicht-48-kHz). Ein Latenz-Feld, das nur eine davon auswertet,
  ist unvollständig.

## 9. Puffer-, CPU- und Gerätelatenz (nicht algorithmisch)

- **Native Engine:** verarbeitet in-place auf dem Graph-Quantum
  (`on_process`, `pipewire_engine.c:280-330`, `position->clock.duration`,
  Fallback 1024). Es gibt **keine** zusätzliche interne Pufferung und keine
  `node.latency`-Deklaration am Filter (Properties nur media.type/category/role,
  node.name, `pipewire_engine.c:361-365`). Die Engine ist laut Tests
  quantum-unabhängig (`test_crystalizer.c:76-96`,
  `test_native_dsp_kernel_parity.py:139-142`).
- **Live gemessen auf der Testmaschine:** `default.clock.rate = 44100`,
  `clock.quantum = 1024`, `min-quantum 32`, `max-quantum 2048`; ALSA-Sink
  `api.alsa.period-size = 1024`, `headroom = 0`. Ein Quantum bei 44,1 kHz sind
  23,2 ms Periodendauer; die wirksame Gerätelatenz ergibt sich aus Quantum ×
  ALSA-Pufferperioden und ist **nicht** code-fixiert. Die von FXRoute verwaltete
  PipeWire-Konfiguration setzt nur `default.clock.rate`/`allowed-rates`
  (`scripts/configure-pipewire-samplerates.sh`), kein Quantum.
- **SRC-Adapter (`native_src/`, für Rate ≠ Graph-Rate):** deklariert
  `latency.ns = 750 ms` bei ungleicher Rate, 0 beim Passthrough
  (`native_src/build.py:133`), 65 536-Frame-Queues, dynamischer
  `timeline_delay = outstanding + (resample_delay + phase)·i/o`
  (`resample-worker.c:82-91`). Telemetrie: `first_output_ns`, `first_real_ns`,
  `lead`, `reserve` (`docs/playback-src.md:168-186`). Die 750 ms sind eine
  **Startup-Reserve**, keine Ziel-Latenz.
- **PCM-Eingang (STDIN):** `pw-cat … --latency 100ms`
  (`audio/stdin_pipewire.py:77`) → Gerätepuffer-Ziel des Ingress-Knotens.
- **Surround-Eingang (PCM/AC-3/DTS durch Helfer):** Live-Ausgabe um **3074 Frames (64,04 ms)**
  verzögert (Abschnitt 6), 32768-Frame-Queues, nur 48 kHz.
- **CPU-Rechenzeit ≠ Latenz:** `FXDSP_TIMING_*` (`timing.c/h`) und
  `max_callback_ms`/`first_output_ns` messen Wanduhr-/CPU-Zeit. Sie verschieben
  keinen Audio-Sample; für die AV-Sync-Bilanz sind sie nur als Nachweis
  relevant, dass Callbacks innerhalb des Quantum-Deadlines bleiben
  (`docs/NATIVE-DSP-PERFORMANCE.md`).

## 10. Vorhandene Lautsprecher-Laufzeitkorrektur (nicht AV-Sync)

Bereits vorhanden und davon zu trennen:

- Per-Output **Align** (`delay_ms` der Output-Bank) als Relativkorrektur
  zwischen Wegen/Subs; die UI hält Werte in −40…+40 ms und verschiebt bei
  Bedarf **alle** Outputs um einen gemeinsamen Offset (`MANUAL.md:393`).
- **AutoSub** und **Speaker Auto Alignment** schreiben in dieselben
  Output-Delays (`measurement/speaker_apply.py`, `measurement/autosub/`).
- **PEQ-Delay-Bänder** (`filterType: "delay"`) werden zu eigenen nativen
  Delay-Stufen (`manager.py:1162-1163,1247-1251`) und erlauben bis 10 000 ms,
  sind aber im gruppen-gebundenen Surround-Global gesperrt.
- Die **Speaker-Align-Werte** sind die einzige *vorzeichenbehaftete* Ablage
  (`modes[mode][processing][role]["alignment_ms"]`, ±40 ms,
  `audio/output_state.py:192`); sie werden im Signal erst über die Sub-Align-
  Umrechnung zu nicht-negativen Delays (siehe 12.10) und landen in derselben
  `output`-Zeile wie alles andere.
- Eine Speaker-Align-Planung, die eine Weg über +40 ms schiebt, **rebased alle
  im Modus gespeicherten Ausgänge um einen gemeinsamen Offset**
  (`measurement/speaker_align.py:405-441`, Validierung in
  `measurement/speaker_commit.py:105-130`) — das bestehende „gemeinsamer
  Offset"-Muster, das die relativen Zeiten zwischen allen Ausgängen erhält.

Diese Offsets sind **Relativkorrekturen zwischen den Ausgängen**. Ein zentraler
AV-Sync-Ausgleich muss davon getrennt bleiben: Der *gemeinsame* Rest
(12.2: `C = max(0, W − T_ref)`) darf die relative Ausrichtung nicht verändern,
und der *Ausgleich* (`D_i`, nur aus der Änderung seit der Kalibrierung) darf die
vom Nutzer bzw. von den Mess-Assistenten gepflegten Align-Werte nicht
überschreiben.

## 11. Meldet heute eine Komponente ihre Latenz? (Punkt 4 des Auftrags)

Nein — im Produktpfad nicht:

- `native_dsp/dsp.h`/`pipewire_engine.c` kennen **keinen** Latenz-Befehl
  (nur `mute`, `inmute`, `peaks get/reset`, `timing get/reset`,
  `effects bypass`, `gain db`, `swap config`, `live *`). Kein Status-Feld,
  keine API, keine UI-Anzeige (Grep über `static/`, `dsp/`, `main.py`:
  nur Doku).
- `fx_lv2_host_latency` (`lv2_host.c:373`) liest den LV2-`out_latency`-Port und
  liefert für den EQ/Limiter/Maximizer korrekte Werte, **aber erst nach dem
  ersten verarbeiteten Block** (vorher 0, gemessen); für die Loudness ist er
  unbrauchbar (4.3). Benutzt wird er nur in Tests.
- `fx_crystalizer_latency`, `fx_upmix_fast_latency`, `fx_upmix_hq_latency`
  sind korrekt implementiert, aber ebenfalls nur Testnutzer.
- Die produktseitige Annahme in `dsp/manager.py:412-425` / `dsp.c:600-620`
  („matched-latency loudness compensation") beschreibt nur das
  **Crossfade-Timing** des Ausgangs-Trims, nicht die Gesamtlatenz.

## 12. Konzept: AV-Sync-Management unter *Calibration*

Keine Implementierung — Zielbild, Datenfluss und Verankerung. Der Anker ist der
vorhandene Workflow-Bereich **Calibration** im Messassistenten
(`static/index.html:1211-1233`: `measurement-calibration-menu` mit SPL
Calibration, Level Matching, Polarity Check; Aktivierung in
`static/app.js` `syncMeasurementCalibrationMenu`).

### 12.1 Ziel, Anforderungen und Abgrenzung

Dieser Abschnitt ist ein **Vorschlag**, keine festgelegte Implementierung.
Offene Architekturentscheidungen sind mit **`OFFEN`** markiert und in 12.11
sowie Abschnitt 13 gesammelt.

Die vier Anforderungen, an denen das Konzept gemessen wird:

1. **AV Sync = Wunsch-Gesamtverzögerung.** Der Nutzer stellt die *Gesamt*laufzeit
   ein; bereits vorhandene Latenzen (Eingang, DSP-Kette, Alignment) werden
   **angerechnet**, nicht zusätzlich zu seiner Zahl addiert.
2. **Time Alignment bleibt erhalten.** Die relative Ausrichtung am Hörplatz darf
   sich nicht ändern — auch nicht durch den Ausgleich.
3. **Unterschiedliche DSP-Pfade** (Kanal/Gruppe, 7.2) werden berücksichtigt,
   ohne bestehende Alignment-Korrekturen aufzuheben.
4. **PCM/AC-3/DTS** müssen entsprechend ihrem tatsächlichen Eingangszweig in
   derselben Bilanz erscheinen; der Live-Helfer verzögert alle drei gleich.

Daraus folgt die tragende Semantik — und die Abgrenzung zur ersten Fassung
dieses Abschnitts, die einen **additiven** AV-Offset beschrieb (`Gesamt =
vorhandene Latenz + Nutzerwert`). Das war falsch: es macht die vorhandenen
Latenzen zu einem Zuschlag statt zu einem Guthaben.

- `W` = **Wunsch-Gesamtverzögerung** des Nutzers (ms). Die Engine ergänzt nur
  den **Rest** gegen das, was die Kette schon liefert.
- Vorhandene Latenz ist ein **Guthaben**: je länger die Kette bereits ist, desto
  weniger fügt FXRoute hinzu.
- Reicht die vorhandene Kette über `W` hinaus, ist `W` **nicht erreichbar**
  (Audio lässt sich nur verzögern, nicht vorziehen): FXRoute klemmt den Zusatz
  auf 0, nennt die Untergrenze `W_min` und erklärt sie, statt still zu klemmen.
- Abgrenzung: Das ersetzt **nicht** Align/AutoSub/Speaker-Alignment
  (Abschnitt 10) und **nicht** die Phasenkorrektur; es stellt beides nicht in
  Frage.

### 12.2 Latenzbilanz: Guthaben, Rest und Ausgleich

Der Compiler (`dsp/manager.py::compile_engine_text`) erzeugt die Config und
kennt Stufen, Paare und Output-Bank. Zusätzlich liefert er die Bilanz. Alle
Größen in ms, intern in Frames der Stream-Rate; `i` läuft über die gerouteten
Ausgänge.

```
für jede Stufe st:  (L(st), reach(st))        reach = global | pairs | output
für jeden Ausgang i:
    L_i = L_upmixer
        + Σ_st (reach(st) deckt i ? L(st) : 0)
        + L_outputbank_i                       (inkl. Korrektur-Baenke)
    E_i = Zweig-Latenz des Eingangs                (12.5)

# 1) Ausgleich: nur die AENDERUNG seit dem Kalibrierzeitpunkt
    Δ_i = (E_i + L_i) − (E_i^k + L_i^k)        k = Stand der letzten Alignment-Messung
    D_i = max_j(Δ_j) − Δ_i                     (>= 0, relativitaetsneutral)

# 2) Guthaben und Rest bis zur Wunsch-Gesamtverzoegerung
    T_i    = E_i + L_i + A_i + D_i             elektronische Gesamtlatenz
    T_ref  = T des Referenzwegs (Front/Main, 12.10.7)
    W_min  = T_ref                             Untergrenze, nicht unterschreitbar
    C      = max(0, W − T_ref)                 nur DIESER Rest wird addiert
    sync_i = D_i + C                           ein Feld, zwei Anteile
```

Zwei Eigenschaften, die die Anforderungen 2 und 3 per Konstruktion erfüllen:

- **(L1) Eine gleichmäßige Änderung ist relativ wirkungslos.** Wächst
   `E_i + L_i` auf allen Ausgängen gleich — z. B. direkter PCM-Pfad → Helfer: 3074 Frames für
  alle —, ist `max_j Δ_j = Δ_i` und damit `D_i = 0` für alle; nur `C` schrumpft.
  Ein Eingangszweigwechsel kann die Ausrichtung also nicht verschieben.
- **(L2) Die Ausrichtung ist invariant.** Mit `D_i = maxΔ − Δ_i` gilt
  `E_i + L_i + D_i = (E_i^k + L_i^k) + maxΔ`: Für jeden Ausgang bleibt genau die
  Relation erhalten, die bei der Kalibrierung galt; `p_i` (Schallflug) und `A_i`
  (Absicht) kürzen sich heraus (Herleitung 12.10.3/12.10.4).

Konsequenz, die in die UI gehört: **Eine Pfadlatenz zu senken bringt nichts
zurück, solange die gemessene Ausrichtung gültig bleibt** — der Ausgleich füllt
die Lücke wieder auf. „Sparen" kann man sie nur, indem man neu misst (dann wird
`A_i` neu abgeleitet) oder die Ausrichtung bewusst verwirft. **`OFFEN`:** Default
„erhalten" (empfohlen) vs. angebotenes „neu vermessen".

- LV2-Beiträge werden **zur Laufzeit nach dem ersten Block** gelesen und auf die
  Paare der Stufe verteilt; bis dahin gilt der Formelwert bzw. „unbekannt".
- Nicht-algorithmische Anteile (Gerätepuffer, Quantum) stehen getrennt
  (Abschnitt 9). **`OFFEN`:** ob sie als Guthaben in `W_min`/`C` zählen
  (Vorschlag: nein, konservativ) oder nur als eigene Zeile gemeldet werden.

### 12.3 Automatische Verrechnung

**Gemeinsamer Weg** — ein zusätzlicher, engine-geführter Delay-Term pro Ausgang,
getrennt vom Nutzer-`Align`:

- Die Output-Bank hat bereits eine Delay-Line (`OUTPUT_DELAY_MAX_MS = 500`,
  `dsp.c:728`), ein Live-Update (`live output`, runtime.py) und sitzt **hinter**
  Matrix/Crossover/Output-Convolver. Sie ist damit der richtige Ort.
- **Zwingend** (nicht nur empfehlenswert, Begründung in 12.10.5): die
  Ausgangsverzögerung im Engine-Zustand **zweiteilen** — `align` (Nutzer/Messung,
  bleibt unangetastet) und `sync` (Engine-Bilanz). Der effektive Leseindex ist
  die Summe; nur `sync` wird vom Compiler bzw. der Bilanz geschrieben.
- **Reihenfolge in der Bilanz:** erst **relativ** ausgleichen (`D_i`, nur die
  Änderung seit der Kalibrierung), dann den **gemeinsamen** Rest `C` auf alle
  Ausgänge legen (`sync_i = D_i + C`). Beide Anteile sind relativitätsneutral:
  `D_i` stellt die alte Relation exakt wieder her, `C` trifft alle Ausgänge
  gleich.
- **Deckelung:** `sync_i` kommt zur bestehenden Align-Verzögerung hinzu; reicht
  der 500-ms-Puffer dann nicht, ist `W` nicht erreichbar. Die Bilanz muss das
  **früh melden** (mit `W_min`) statt still zu klemmen.

**Die drei Modi** (unter der Guthaben-Semantik aus 12.2):

1. **Stereo** (zwei Ausgänge, L/R): In der Regel tragen beide dieselbe
   `E + L`, dann ist `D_i = 0` und nur `C` wirkt. Unterschiede entstehen bei
   einseitiger Korrektur (Convolver/FIR nur auf einer Seite) oder wenn
   Crossover-Wege auf verschiedene Ausgänge gehen; dann greift `D_i` pro
   Ausgang — ohne den Align-Wert anzufassen. Größte Posten sind hier die
   linearphasige Korrektur-IR (`N/2`, Abschnitt 5) und die Loudness-FFT-Größe.
2. **Stereo + Sub** (`subwoofer-2.1/2.2/2.2-stereo`): Die *gewollte*
   Sub-Ausrichtung steckt in `sub_alignment_ms`/`sub2_alignment_ms`
   (`dsp/runtime.py:221-238`) und in den Align-Werten; sie ist Absicht und wird
   nie kompensiert. `D_i` reagiert nur auf `Δ`: Wird z. B. nur der Sub-Zweig
   länger (eigener Convolver/EQ), dann ist `D_sub = 0` und die **Mains** werden
   um dasselbe Maß aufgefüllt — die eingemessene Relation bleibt exakt erhalten.
   Eine Änderung der Sub-Ausrichtung dagegen ist eine Absichtsänderung und
   ändert `A`, nicht `Δ`. Die Untergrenze `W_min` wird vom **Main-Pfad**
   definiert (Referenzweg), nicht vom Sub.
3. **Surround** (bis 8 Ausgänge): Der Upmixer ist gemeinsam (kürzt sich aus der
   relativen Bilanz heraus), gruppen-gebundene Plugins (`pairs`) und die
   Output-Bank erzeugen die Spreizung (7.2). Beispiel: Ein FIR-EQ auf `pairs 1`
   (FL/FR) erhöht `Δ` für FL/FR um 6144 Frames; FC/LFE/SL/SR/BL/BR werden dann
   um 6144 aufgefüllt, sofern die Ausrichtung davor gemessen wurde und erhalten
   bleiben soll. Unter der Guthaben-Semantik sinkt `C` entsprechend — `W` bleibt
   erreichbar, aber `W_min` steigt um 128 ms. Wer die 128 ms wirklich sparen
   will, muss die Ausrichtung neu messen (`OFFEN`, 12.2).

### 12.4 Wunsch-Gesamtverzögerung (Nutzer-Eingabe)

- Ein Zahlenfeld **„Gesamtverzögerung Audio"** pro **Output-Modus** (nicht pro
  Preset/Bank — analog zur Level-Matching-Ablage, `MANUAL.md:415-419`, die
  bewusst „per output mode" speichert).
- **Semantik:** `W` ist die **Gesamtlatenz**, nicht ein Zusatz. Anzeige immer
  mit Aufschlüsselung, z. B. „Gesamt: 260 ms · davon in der Kette: 210 ms
  (Eingang 171, DSP-Stufen 39, Alignment 0) · FXRoute ergänzt: 50 ms".
- **Bereich:** `W_min … W_min + 500 ms`, wobei `W_min` die bereits vorhandene
  Gesamtlatenz des Referenzwegs ist und 500 ms `OUTPUT_DELAY_MAX_MS`
  (`dsp.c:46`) entsprechen. Eine Eingabe unter `W_min` wird **erklärt statt
  übernommen**: Audio kann nicht vorgezogen werden; entweder die Kette wird
  kürzer (Preset/Korrektur) oder das Ziel bleibt unerreichbar.
- Der Wert wird in **ms** gespeichert und beim Compile mit der aktuellen Rate in
  Frames umgerechnet — nötig, weil die LV2-/Loudness-/EQ-Latenzen in **Frames**
  fest sind und ihr ms-Wert mit der Rate skaliert (4.2/4.3). Eine Speicherung in
  Frames würde bei 44,1 ↔ 48 ↔ 96 kHz still verrutschen.
- **`OFFEN` — Ebene der Speicherung:** pro Output-Modus vs. pro Eingangszweig.
  Die *Anrechnung* ist zweigabhängig, der Nutzerwunsch eher nicht; Vorschlag:
  pro Modus speichern, in der UI die effektive Zahl je Zweig zeigen.
- **`OFFEN` — Video-Gegenwert:** Darf der Video-Zweig mitziehen (Bild später),
  wäre auch `W < W_min` sinnvoll. Im Player-Pfad existiert dafür heute kein
  Parameter (12.8, Abschnitt 11).

### 12.5 Eingangs-/Zweigabhängigkeit

Die Audio-Latenz hängt nicht nur am DSP, sondern am gewählten **Zweig**:

| Zweig | `E` = Guthaben (`W`-Anrechnung) | Quelle |
|---|---:|---|
| PCM-Eingang / Datei-Wiedergabe | 0 (Gerätepuffer separat, Abschnitt 9) | `audio/stdin_pipewire.py:77` |
| Digital-In komprimiert (AC-3/DTS) | **3074 Frames = 64,04 ms** | 6.2 |
| Digital-In PCM durch Surround-Helfer | **3074 Frames = 64,04 ms** | 6.2 |
| SRC-Adapter aktiv (Rate ≠ Graph-Rate) | dynamisch (`timeline_delay`), nicht stabil | `native_src/build.py:133`, `docs/playback-src.md` |
| Nicht-48-kHz-Graph am Surround-Eingang | Stille (fail closed) — keine Bilanz | `ingress.c:100-101` |

Konsequenz für das Konzept: Der Eingangszweig ist unter der Guthaben-Semantik
kein Zusatzproblem, sondern ein **automatischer Abzug von `C`**. Wechselt der
Nutzer vom direkten PCM-Pfad zum Live-Helfer, sinkt `C` um 64,04 ms, die
Gesamtverzögerung bleibt bei erreichbarem Ziel `W`, und die relative Ausrichtung
ändert sich nicht (Lemma L1, 12.2). PCM ↔ AC-3/DTS innerhalb des Helfers verändert
das Guthaben nicht. Voraussetzungen:

- Der Zweig muss **bekannt** sein: `state` (`pcm` / `encoded` / `muted`) und
  `sample_rate` aus dem Status des Eingangs-Helfers.
- `C` muss beim Zweigwechsel **innerhalb derselben Transaktion** neu gerechnet
  werden; die sichere Bitstream→PCM-Lücke beträgt je nach Burst etwa 107–128 ms;
  PCM→Bitstream verwirft keinen etablierten PCM-Tail (6.4).
- **`OFFEN` — SRC-Zweig:** Die effektive Latenz folgt `timeline_delay` und ist
  situationsabhängig; ein Guthaben daraus wäre nicht stabil. Vorschlag: SRC
  nicht anrechnen (konservativ, `C` bleibt größer) und als eigene Zeile melden.
- **`OFFEN` — Decoder-Zeitraster:** AC-3 rastet in 32 ms, DTS in 10,67 ms
  (6.1). Das ist keine Zusatzlatenz, sondern das Zeitraster des Formats;
  Vorschlag: nicht anrechnen, nur dokumentieren.

### 12.6 Bedienung unter *Calibration*

- Neuer Menüpunkt neben SPL Calibration / Level Matching / Polarity Check:
  **AV Sync** (`measurement-calibration-menu`, `static/index.html:1211-1233`).
- Inhalt:
  1. **Aufschlüsselung der Gesamtlatenz:** „Gesamt: W ms · in der Kette: X ms
     (Eingang / Upmixer / Stufen / Output-Bank / Alignment) · FXRoute ergänzt:
     C ms".
  2. **Bereich und Untergrenze:** `W_min` (bereits vorhandene Latenz des
     Referenzwegs) und `W_min + 500 ms` als Obergrenze; eine Eingabe unter
     `W_min` wird mit Begründung abgelehnt statt still geklemmt.
  3. **Eingabefeld Wunsch-Gesamtverzögerung**, gespeichert pro Output-Modus.
  4. **Spreizungszeile:** „Kanal/Gruppe X hat Δ = … ms; Ausgleich ergänzt … ms"
     plus der Hinweis, dass dabei **kein Align-Wert** verändert wird.
   5. **Zweigzeile:** „Digital-In (Dolby Digital 5.1): 64,04 ms angerechnet".
  6. Anzeige „angewandt" / „gedeckelt" / „nicht erreichbar" (jeweils mit Grund).
  7. Hinweis, dass der Wert nur die Audio-Seite verzögert und dass strenge
     Lip-Sync-Ziele den Video-Zweig brauchen (12.4).
- Statuszeile wie bei den anderen Kalibrierungen (Erfolg/Nicht-verifiziert/
  nichts angewandt) statt stiller Wirkung.

### 12.7 Erkennung und Nachführung

- **Auslöser:** jeder Compile (Preset-, Modus-, Rate-, Layout-Wechsel), jede
  Live-Transaktion (8.2) und jede Änderung des Engine-Status (Bypass, Mute,
  aktive Rate). Nach jedem dieser Ereignisse die Bilanz neu rechnen und `sync`
  in derselben Transaktion setzen.
- **Verifikation:** Nach dem Anwenden die Bilanz gegen den Engine-Status
  gegenprüfen (Bypass aktiv? Upmixer aktiv? Loudness-Modus?) — die Config allein
  genügt nicht.
- **Nachführung bei Lautsprecher-Messungen:** Sweeps messen Ankunftszeiten,
  und die Ankunft enthält die algorithmische Latenz. Der Ausgleich darf deshalb
  **nicht** neutralisiert werden, sondern ist der Bezugspunkt der Messung — die
  Herleitung steht in 12.10.4. Der **gemeinsame** Rest `C` (12.2) ist für
  Relativmessungen unkritisch (er trifft alle Ausgänge gleich). Zusätzlich muss
  jede Latenzänderung einen **laufenden** Messjob abbrechen: AutoSub erkennt eine
  Ankunftsverschiebung erst ab `max(4096, rate/12)` Samples
  (`measurement/autosub/measurement.py:1475-1521`, ≈ 85,3 ms bei 48 kHz) —
  kleinere Sprünge (1024/2048 Samples = 21,3/42,7 ms) liegen innerhalb der
  Toleranz und verschieben die Basis still.
- **Kein automatisches Plugin-PDC:** Das Loudness-/EQ-/Crystalizer-Delay ist
  gewünschtes Verhalten (Filter schauen voraus) und wird **nicht** wegkompensiert
  — ausgeglichen werden nur die *Unterschiede* zwischen Kanälen sowie der
  Abstand zum Video.

### 12.8 Was dafür fehlt

- **Kein AV-/`audio-delay`-Parameter im Player-Pfad** (Grep über `playback/`,
  `main.py`: keine Treffer). Die zu meldende Zahl ist die Gesamtlatenz des
  gewählten Audio-Zweigs, nicht nur die der Extras.
- Latenz-Ausgabe im Native-Engine-Status (neues Feld neben `global_extras`),
  damit Bypass/Mute/aktive Stufe sichtbar sind.
- Ein zweiter, engine-geführter Delay-Term pro Ausgang (12.3), damit Messung und
  AV-Sync sich nicht überschreiben.
- Latenz-Ausgabe des Eingangs-Helfers (vorhandene Felder `lookahead_frames`,
  `state` plus `live_latency_frames=3074`) in derselben Bilanz.

### 12.9 Umsetzung in Stufen (Vorschlag)

1. **Bilanz ohne Wirkung:** Compiler rechnet `E_i`, `L_i`, `T_i`, `W_min` und
   `Δ`, Status/API liefert sie, UI zeigt die Aufschlüsselung. Kein Eingriff in
   den Audio-Pfad → risikofrei prüfbar.
2. **Ausgleich:** zweiter Delay-Term pro Ausgang, Anwendung im selben Commit.
   Der Ausgleich bleibt während Sweeps **aktiv** (er ist der Bezugspunkt der
   Messung, 12.10.6); zusätzlich wird die Latenz-Basis `L^k` (L-Vektor und
   beteiligte Stufen) neben dem Plan-Fingerprint gespeichert.
3. **Eingangszweige:** Surround-/SRC-/PCM-Beiträge in die Bilanz und in die UI.
4. **Wunsch-Gesamtverzögerung:** Feld unter Calibration (Speicherung pro
   Output-Modus); die Bilanz rechnet `W_min` und den Rest `C` als Guthaben und
   zeigt die Aufschlüsselung. Übergabe an den Video-Zweig, sobald dort ein
   Offset-Parameter existiert.
5. **Nachführung/Warnungen:** Hinweis bei Latenzsprüngen (Live-Änderungen nach
   8.2, Bypass), Warnung „Alignment veraltet" sobald sich die algorithmische
   Latenz seit der letzten Messung geändert hat (12.10.6), und bei Deckelung.

### 12.10 Zusammenspiel mit dem vorhandenen Time Alignment

Die Time Alignment des Hörplatzes ist **Absicht**, nicht Fehler. Die Bilanz
muss sie deshalb als Referenz behandeln und darf sie nicht mitkompensieren. Die
folgende Herleitung ist der Kern der Antwort auf die Frage, wie die vorhandenen
Lautsprecher-Delays in die Gesamtlatenz eingehen, ohne doppelt kompensiert zu
werden.

#### 12.10.1 Wie die Lautsprecher-Delays heute im Signal liegen

Es gibt **eine** Verzögerungsablage pro Ausgang, und alle Absichten münden in
sie:

```
BassManagementConfig (dsp/runtime.py:221-238)
  derived_main_delay_ms  = max(0, -min(sub_alignment_ms, sub2_alignment_ms))
  derived_sub1_delay_ms  = derived_main_delay_ms + sub_alignment_ms   (2.2)
  derived_sub2_delay_ms  = derived_main_delay_ms + sub2_alignment_ms  (2.2)
        |
        v  dsp/runtime.py:363-372
  layout[FL/FR].delay_ms = derived_main_delay_ms
  layout[SUB1/2].delay_ms = derived_sub{1,2}_delay_ms
        |
        v  dsp/manager.py:1255
  "output <index> <gain_db> <delay_ms> <polarity>"
        |
        v  native_dsp/dsp.c:701,1199-1201 (delay_line, OUTPUT_DELAY_MAX_MS 500)
```

Wichtig: Eine **negative** Sub-Ausrichtung wird nicht durch „Sub früher"
realisiert, sondern durch ein **positives Delay auf die Mains**
(`derived_main_delay_ms`). Die gespeicherten Align-Werte sind relativ und
vorzeichenbehaftet, die Engine sieht nur nicht-negative absolute Delays.
Damit ist diese Ablage bereits die Summe aller bisherigen Timing-Absichten:
Align-Werte, Sub-Ausrichtung, AutoSub-Ergebnis und der gemeinsame Rebase nach
+40 ms. Es gibt heute **keinen** zweiten Delay-Term im Ausgangspfad.

Konsequenz für die Bilanz: Für die *absolute* Latenz eines Ausgangs ist dieser
Wert eine reale Verzögerung und muss mitgezählt werden. Für den *Ausgleich* ist
er die Zielvorgabe und darf nicht verändert werden.

#### 12.10.2 Was die Messung tatsächlich sieht

Die Ankunftszeit wird im Messpfad gegen einen **Referenzzeitpunkt** bestimmt
(`measurement/host_capture.py:566-580`: `reference_arrival_index` /
`reference_peak_index` bzw. der Host-Referenzpfad; `timing_status` `valid`).
Fahrlässig wäre, diesen Referenzzeitpunkt als Zeitanker für die *Ausrichtung* zu
nehmen — `measurement/speaker_service.py:134-152` benutzt ihn ausdrücklich
nicht, sondern den **Referenzweg** (den spätesten Weg, der undelayed bleibt):
„The electrical reference is no such anchor: its loopback may carry a way whose
delay changed". Das Produkt schützt sich hier bereits davor, dass die eigene
Kette den Zeitanker verschiebt.

Die Planung ist dann rein **paarweise relativ**
(`measurement/speaker_align.py:379-381`):

```
reference_role = max(arrival)
latest         = arrival[reference_role]
added_delay    = {role: latest - arrival[role]}
```

Daraus folgt die entscheidende Tatsache: Die Ankunft enthält die
algorithmische Latenz des Pfades. Ein Weg mit zusätzlicher Plugin-Latenz
erscheint **jede** Messung später, also steckt dieser Unterschied anschließend
in den gespeicherten Align-Werten. Die vorhandene Lautsprecher-Korrektur
entsteht also *messend* und kann die algorithmische Spreizung bereits
enthalten — sie ist damit eine **latente Kompensation**, die man nicht ein
zweites Mal anwenden darf.

Dasselbe gilt für AutoSub: es scannt Kandidaten um die gespeicherte
Sub-Ausrichtung und überwacht die Ankunft als Chain-Health-Größe
(`measurement/autosub/measurement.py:1475-1521`).

#### 12.10.3 Die Doppelkompensations-Falle (zwei Varianten)

Mit `p_i` = Schallflugzeit, `A_i` = gespeicherter Align-Anteil, `L_i` =
algorithmische Latenz, `D_i` = Ausgleich, `C` = gemeinsamer Rest bis zur
Wunsch-Gesamtverzögerung (12.2):

```
t_i (Ankunft am Hörplatz) = p_i + A_i + L_i + D_i + C
```

**Falle 1 — Gesamtlatenz egalisieren.** Wer die Summe `A_i + L_i` bildet und
auf das Maximum auffüllt, rechnet

```
D_i = max_j(A_j + L_j) - (A_i + L_i)
```

und zieht damit `A_i` auf beiden Seiten heraus: Der Ausgleich verwandelt die
gewollte Ausrichtung in eine Gleichverteilung und **zerstört die Korrektur am
Hörplatz**. Das ist der offensichtliche Fehler.

**Falle 2 — absolute Latenz egalisieren, obwohl sie schon gemessen wurde.**
Wer die Align-Werte als „rein akustisch" behandelt und `D_i = L_max - L_i`
setzt, wendet den Unterschied zweimal an (einmal steckt er schon in `A_i`, weil
die Messung ihn sah, einmal jetzt in `D_i`). Ergebnis: genau die doppelte
Verschiebung, die vermieden werden soll — und zwar nur auf den Kanälen, deren
Pfade unterschiedlich lang sind, also unbemerkt in der Gruppen-/Sub-Ausrichtung.

#### 12.10.4 Die Invariante und die Rechenregel

Die Ausrichtungs-Messung hat (mit `D = 0` und der damaligen Latenz `L^k`, „k"
wie Kalibrierzeitpunkt) festgelegt:

```
p_i + A_i + L^k_i = r_i        (r_i = gewollte Ankunftsrelation des Verbunds)
```

Gefordert ist, dass `r_i` unverändert bleibt, also `t_i` dieselbe Relation
zeigt:

```
p_i + A_i + L_i + D_i + C = r_i  fuer alle i des Verbunds
=>  D_i + (L_i - L^k_i) + C = konstant ueber i
```

Daraus folgen drei Regeln:

1. **Ausgeglichen wird die *Änderung* der algorithmischen Latenz, nicht die
   absolute Latenz** (und nicht die Summe mit `A_i`). Ein *gemeinsamer*
   Zusatzbedarf ist für die relative Ausrichtung wirkungslos und gehört in den
   gemeinsamen Rest `C`, nicht in `D`.
2. **Minimale nicht-negative Lösung:**
   `D_i = max_j(L_j − L^k_j) − (L_i − L^k_i)`. Wächst die Latenz auf allen
   Pfaden gleich, ist `D_i = 0` für alle — die Ausrichtung kann per Konstruktion
   nicht kaputtgehen.
3. **Ohne bekannte Basis `L^k` ist `D = 0` der sichere Standard.** Das ist
   genau der heutige Zustand: Die vorhandene Korrektur bleibt unangetastet, und
   erst eine *Änderung* der Latenz relativ zum Kalibrierzeitpunkt wird
   ausgeglichen. Statt zu raten, muss die Basis aufgezeichnet werden.

Merksatz: **Ausgeglichen wird, was der Nutzer nicht in einem Timing-Feld
benennen kann; erhalten bleibt, was ein Timing-Feld hat.**

- *Absicht (nie ausgleichen):* Output-Align, Sub-Ausrichtung, AutoSub-Ergebnis,
  der gemeinsame Rebase, PEQ-Delay-Bänder, ein bewusster L/R-Versatz einer
  `minimum_aligned`-Korrektur.
- *Fehler/Engine-Anteil (ausgleichen):* Block-/FFT-Latenz von EQ, Loudness,
  Limiter, Crystalizer, Upmixer; der Verzögerungsanteil eines
  linearphasigen Korrektur-IR (`N/2`); die Eingangs-/Decoder-Reserve.

**Verallgemeinerung (12.2):** Der Eingangszweig geht in dieselbe Größe ein —
`Δ_i = (E_i + L_i) − (E_i^k + L_i^k)`. Die Herleitung hier ist der Fall ohne
Zweigwechsel; für einen Wechsel gilt Lemma L1: uniformes `Δ` ⇒ `D_i = 0`, nur
`C` ändert sich.

#### 12.10.5 Warum der Ausgleich einen eigenen Slot braucht

Zwei Mechanismen schreiben heute dieselbe `output`-Zeile, und keiner davon
lässt einen dritten zu:

- Die Alignment-Planung **rebased alle** gespeicherten Ausgänge des Modus um
einen gemeinsamen Offset, sobald ein Wert über +40 ms läuft
  (`measurement/speaker_align.py:405-441`) — hätte der Ausgleich daneben in
  `alignment_ms` gestanden, würde er mitverschoben.
- Die Commit-Validierung akzeptiert nur Kandidaten, die **ausschließlich**
  `alignment_ms`/`level_db` ändern, und verlangt für nicht geroutete Rollen
  exakt den gemeinsamen Offset (`measurement/speaker_commit.py:79-130`); ein
  hineingerechneter Latenzausgleich wäre ein Fremdkörper und würde abgelehnt.
- Das Fenster ist ohnehin zu klein: ±40 ms (`audio/output_state.py:192`) gegen
  eine Spreizung, die ein einzelner FIR-EQ schon mit **128 ms** füllt
  (Loudness 16384 allein: 341 ms).

Deshalb: eigenes `sync`-Feld pro Ausgang im Engine-Zustand, Summe im Leseindex
der Delay-Line, geschrieben nur von der Bilanz, und aus jedem Mess-/
Neutralisationspfad ausgenommen — während ein Sweep läuft, ist der Ausgleich
aktiv (12.10.6), aber seine Werte stammen nie aus einer Messung.

#### 12.10.6 Messung mit oder ohne Ausgleich — und die Basis

Weil die Ankunftsmessung die Pfadlatenz *mitmisst* (12.10.2), gibt es nur zwei
konsistente Betriebsarten:

- **Ausgleich aktiv während der Messung** (empfohlen): Der Sweep sieht einen
  bereits egalisierten Stand, `A_i` wird rein akustisch, und jede künftige
  Latenzänderung relativ dazu ist ein sauberer Ausgleichsfall.
- **Ausgleich neutralisiert während der Messung:** Dann enthält `A_i` die
  algorithmische Spreizung von damals; ein danach aktiver Ausgleich
  **verdoppelt** sie (Falle 2). Zulässig nur mit aufgezeichneter Basis, aus der
  die Spreizung wieder herausgerechnet wird.

Für die Einführung ohne Verhaltensänderung: beim ersten Aktivieren gilt
`L^k := L_jetzt` (Ergebnis `D ≡ 0`), die bestehende Korrektur bleibt exakt
erhalten. Die Basis (`L`-Vektor und die beteiligten Stufen) gehört zusammen mit
`processing_fingerprint`/`plan_fingerprint` gespeichert — dieselbe
Fingerprint-Disziplin, die der Messpfad heute schon gegen den gerenderten Plan
prüft (`expected_plan_fingerprint`, `expected_native_context` in
`measurement/level_match_runtime.py:136-147`).

**Vorhandener, bislang unsichtbarer Fall:** Der Alignment-Plan rendert die
Global-/Area-PEQ-und-Convolver-Bänke **neutralisiert**
(`measurement/speaker_commit.py:62-72`), die Korrektur ist danach aber aktiv.
Deren Latenzbeitrag entsteht also *nach* der Messung. Solange beide Seiten
dieselbe Tap-Länge und denselben Phasenmodus nutzen (der Stereo-Ablauf wählt
einen gemeinsamen Wert), ist der Unterschied 0 und die Ausrichtung bleibt. Wird
eine Korrektur nur auf einem Pfad oder auf einer Gruppe aktiv, verschiebt sie
die relative Ankunft um bis zu `N/2` (IR) bzw. 6144 (FIR-EQ) Samples — genau
der Fall, für den die Bilanz gebaut wird.

#### 12.10.7 Die AV-Gesamtzahl

- Zu melden ist die **elektronische Gesamtlatenz des Referenzpfads** (Default:
  Front/Main, sonst der früheste geroutete Ausgang) inklusive Eingang und
  Align-Anteil, denn dieser Wert verzögert den Ton tatsächlich:
  `T_ref = E_ref + L_ref + A_ref + D_ref`; die Schallflugzeit `p_i` und
  DAC/AVR/TV liegen außerhalb des Codes (Abschnitt 13). Genau diese Zahl ist das
  **Guthaben** gegen die Wunsch-Gesamtverzögerung: `W_min = T_ref`.
- Der gemeinsame Rest `C = max(0, W − T_ref)` (12.2) trifft alle Ausgänge gleich
  und berührt die relativen Zeiten nicht. **Kapazität:** Für jeden Ausgang gilt
  `A_i + D_i + C ≤ 500 ms` (`OUTPUT_DELAY_MAX_MS`, `dsp.c:46`), also
  `W ≤ T_ref + 500 ms − max_i(A_i + D_i)`. Wird diese Grenze überschritten oder
  `W < W_min` gefordert, ist `W` **nicht erreichbar**; die Bilanz meldet das,
  statt zu klemmen (12.4).
- Ein AutoSub-/Speaker-Align-Lauf kann `A_i` einzelner Ausgänge und den
  **gemeinsamen Rebase** verändern. Der Rebase verschiebt alle Ausgänge gleich,
  ist also akustisch wirkungslos, ändert aber `T_ref` und damit `W_min` als Zahl
  — die AV-Gesamtzahl und der Rest `C` müssen deshalb nach jedem
  Alignment-Commit aus den **gespeicherten** Werten neu gerechnet werden, nicht
  aus einer normierten Relativform.

### 12.11 Prüfung des Konzepts gegen die vier Anforderungen

| # | Anforderung | Umsetzung | Verdikt |
|---|---|---|---|
| 1 | AV Sync = Wunsch-Gesamtverzögerung, vorhandene Latenzen angerechnet | `T_i = E_i + L_i + A_i + D_i`, `C = max(0, W − T_ref)`, `W_min = T_ref` (12.2/12.4) | **korrigiert.** Die erste Fassung addierte einen Offset auf die vorhandene Latenz (`Gesamt = vorhanden + Wert`) und ließ die Kette damit als Zuschlag dastehen. Jetzt ist sie ein Guthaben; `W < W_min` wird als nicht erreichbar gemeldet. |
| 2 | Time Alignment bleibt erhalten | `D` nur aus `Δ` seit der Kalibrierung; `A` wird nie geschrieben; `C` trifft alle Ausgänge gleich; Lemma L2 | erfüllt, per Konstruktion (12.2/12.10.4). Zusatzregel: `D`/`C` sind keine `align`-Schreiber (12.10.5). |
| 3 | Unterschiedliche DSP-Pfade berücksichtigt, ohne Alignment aufzuheben | `Δ_i` je Pfad, `pairs`-Reichweite (7.3), `D_i = maxΔ − Δ_i` | erfüllt; **korrigiert** gegenüber `delta(i) = L_max − L_i` (absolute Spreizung), das eine bereits eingemessene Spreizung verdoppelt hätte (Falle 2, 12.10.3). |
| 4 | PCM/AC-3/DTS-Eingangslatenzen berücksichtigt | `E_i` in `T_i` und `Δ_i`; Lemma L1 | erfüllt: ein Zweigwechsel ändert nur `C`, nie die relative Ausrichtung. Offen bleiben SRC-Zweig und Decoder-Zeitraster (12.5). |

Zur vorgeschlagenen **`align`/`sync`-Trennung** selbst:

- Die **Trennung ist erforderlich** — nicht wegen der Formel, sondern wegen der
  Schreibpfade: Die Alignment-Planung rebased und überschreibt `alignment_ms`
  (`speaker_align.py:405-441`), die Commit-Validierung lässt nur
  `alignment_ms`/`level_db` zu (`speaker_commit.py:79-130`), und das Fenster ist
  ±40 ms (`audio/output_state.py:192`). Ein Bilanzwert kann dort nicht liegen.
- Der **Inhalt** von `sync` war falsch gefasst: nicht „absolute Spreizung"
  (`L_max − L_i`), sondern `D_i + C` = (Änderung seit der Kalibrierung) +
  (Rest bis zur Wunsch-Gesamtverzögerung).
- Was `sync` **nicht** sein darf: ein zweiter Schreiber von `A_i` und keine
  Quelle für Messwerte (12.10.5/12.10.6).

## 13. Offene Punkte / Unsicherheiten

1. **Physische Latenz** von DAC/AVR/TV (HDMI/eARC) ist nicht Teil des Codes und
   muss extern bestimmt werden (Messmikrofon oder Geräteangabe). Sie ist der
   größte verbliebene unbekannte Anteil der Gesamtbilanz.
2. **Live-Pfad des Surround-Eingangs:** Der aktuelle Offset von 3074 Frames
   ist im privaten Graphen bei 48 kHz inklusive Schnellwechseln und CPU-Last
   sowie passiv im Produktgraphen am aktiven TV-PCM geprüft (je 30 s ohne/mit
   sechs Busy-Workern, keine Verluste, mindestens 16,268 ms Worker-Reserve).
   Der Beobachterhop ist getrennt von der algorithmischen Latenz;
   physische TV-/ADC-/DAC-Latenz bleibt ungemessen (6.5).
3. **LSP-Loudness-Port**: Ursache des konstanten 7680 und des Nachhinkens ist
   aus dem TTL nicht erklärbar (gemessen, 4.3). Für die Bilanz ist nur die
   Formel „Latenz = fftSize" zu verwenden.
4. **LSP-FIR-Warm-up**: Der erste Block nach `activate` liefert keinen Ausgang
   (4.2); ob das für alle LSP-Plugins gilt und ob ein Live-Wechsel kurzzeitig
   zusätzliche Latenz erzeugt, ist nur für den EQ gemessen.
5. **Upmixer-Moduswechsel** (`param mode`, fast ↔ hq) ist als Live-Wert
   klassifiziert; ob die Native-Engine ihn wirklich live übernimmt oder in den
   Rebuild fällt, ist nicht gemessen.
6. **SRC-Adapter**: nominelle 750 ms sind ein **Startup-Reserve**, keine
   Ziel-Latenz (`docs/playback-src.md:139-143`); die effektive Live-Latenz folgt
   `timeline_delay` und ist situationsabhängig.
7. **Gruppen-gebundene Plugins** im Surround-Global und der Output-Bank-Budget-
   Check (`OUTPUT_FILTER_MAX_BIQUADS`) begrenzen, welche Pfade überhaupt
   unterschiedlich lang werden können.
8. **Nicht-48-kHz-Decoderpfad** existiert nicht (fail closed) — ein künftiger
   Bedarf (96 kHz AC-3) würde die Bilanz um einen zweiten Eingangswert erweitern.
9. **Keine Latenz-Basis neben dem Time Alignment:** Es gibt heute keinen
   gespeicherten Wert dafür, welche algorithmische Latenz beim letzten
   Alignment-/AutoSub-Lauf aktiv war (12.10.6). Ohne ihn ist `D = 0` der einzige
   sichere Standard; mit ihm wäre auch ein nachträglicher Ausgleich möglich.
10. **Nicht modellierte, akustische und Geräte-Anteile:** `p_i` (Schallflug),
    DAC/AVR/TV und der Gerätepuffer stehen außerhalb des DSP-Modells. Die
    Bilanz liefert nur die elektronische Kette; für Lip-Sync muss der Rest
    extern bestimmt werden.
11. **LV2-Ports im Messpfad:** Wird ein Instrument verlangt, das die
    tatsächliche Latenz *misst* (statt zu rechnen), ist der Port erst nach dem
    ersten Block gültig und für die Loudness unbrauchbar (11).
12. **`OFFEN` — Ausgleich vs. Neuvermessung:** Bei geänderter Pfadlatenz
    entweder automatisch `D_i` anwenden (Ausrichtung bleibt exakt, Latenz wird
    **nicht** gespart) oder warnen und neu messen lassen (Latenz wird gespart,
    die Ausrichtung wird neu abgeleitet). Empfehlung: automatisch ausgleichen,
    Neuvermessung als bewusste Aktion anbieten (12.2/12.3).
13. **`OFFEN` — Guthaben-Umfang:** Zählen der Gerätepuffer/Quantum (Abschnitt 9)
    und die effektive SRC-Latenz als Guthaben in `W_min`/`C`, oder werden sie
    konservativ nur gemeldet? Vorschlag: nur deterministische Anteile anrechnen,
    den Rest separat ausweisen (12.2/12.5).
14. **`OFFEN` — Ebene und Video-Seite:** `W` pro Output-Modus oder pro
    Eingangszweig (12.4); und ob der Video-Zweig einen Gegenwert bekommt,
    womit auch `W < W_min` zulässig würde.
15. **`OFFEN` — Referenzweg der Bilanz:** Front/Main als Default oder der
    früheste geroutete Ausgang (12.10.7) — hängt davon ab, welcher Kanal das
    Programm führt.

## 14. Anhang: Reproduktion der Messungen

```sh
# Engine bauen (Testmaschine hat die nativen Abhängigkeiten)
cd ~/fxroute && sh native_dsp/build.sh && sh native_surround/build.sh

# 1. LV2-Ports (deklariert)
sh native_dsp/test_lv2_host.sh

# 2. Impulslatenz je Stufe: Impuls (Frame 0) → fxroute-dsp-offline
#    native_dsp/build/fxroute-dsp-offline CONFIG IMPULS.f32 AUS.f32 1024
#    Peak-Position der Ausgabe = Latenz in Frames.
python3 /tmp/fxmeasure2.py      # Loudness/Maximizer/EQ/Upmixer/Delay-Kette, Impuls
python3 /tmp/fxcry.py           # Crystalizer über acht Sampleraten

# 3. Zweiter/dritter Schätzer (Kreuzkorrelation, Least-Squares-Lag)
cc -O2 -I native_dsp /tmp/fxxcorr.c native_dsp/lv2_host.c -o /tmp/fxxcorr \
   $(pkg-config --cflags --libs lilv-0) -lm -lpthread && /tmp/fxxcorr
cc -O2 -I native_dsp /tmp/fxeqsse.c native_dsp/lv2_host.c -o /tmp/fxeqsse \
   $(pkg-config --cflags --libs lilv-0) -lm -lpthread && /tmp/fxeqsse   # EQ-Modi
cc -O2 -I native_dsp /tmp/fxloudsse.c native_dsp/lv2_host.c -o /tmp/fxloudsse \
   $(pkg-config --cflags --libs lilv-0) -lm -lpthread && /tmp/fxloudsse # Loudness
/tmp/fxeqoff                    # Impulsposition (Warm-up-Artefakt), 4.2
/tmp/fxlivectl                  # Live-Control-Wechsel fft, 8.2

# 4. Reale Produktketten: DSPManager.compile_engine_text(...) + Offline-Engine
#    mit dem App-Venv (numpy):
cd ~/fxroute && .venv/bin/python3 /tmp/fxchain.py
python3 /tmp/fxsurround.py      # Gruppen-/Output-Spreizung (7.2)

# 5. Surround-Decoder (AC-3/DTS): IEC-61937-Träger aus ffmpeg-Fixtures
python3 /tmp/fxdec2.py          # Burst-Geometrie, 1:1-Abbildung, PCM-Übergang
python3 /tmp/fxdec3.py          # Codec-/Layoutwechsel, Latch-Release, Mutelängen
# Einzelmessung:
#   ffmpeg -v error -f lavfi -i aevalsrc=...:s=48000:d=2:c=5.1(side) \
#          -c:a ac3 -strict -2 -b:a 640k -f spdif pipe:1 > /tmp/t.spdif
#   native_surround/build/fxroute-surround-input --offline IN.s32 OUT.f32 \
#          --status STATUS.json --chunk 257
#   (Träger = 16-Bit-Wörter in den oberen 16 Bit eines S32-Rahmens)

# 6. Live-Graph
pw-metadata -n settings 0 | grep -E "clock.(rate|quantum)"
pw-dump | python3 -c 'import json,sys; ...'   # node.name/node.latency/api.alsa.*
python3 scripts/test_surround_ingress.py
```

Zentrale Codefundstellen:
`native_dsp/dsp.c:30-46` (Konstanten), `:294-380` (Convolver),
`:433-443` (Upmixer-Reset), `:550-556`/`:701`/`:728`/`:1000` (Delays),
`:600-620`/`:1050-1070` (Loudness-Trimmung), `:87-177`/`:653` (Pair-Mask),
`:1021` (Live-Updates), `:1170-1210` (Output-Bank), `:1211-1285` (Dispatch),
`native_dsp/upmix_fast.c:98-128`, `native_dsp/upmix_hq.c:227`,
`native_dsp/crystalizer.c:414-419,580-584`, `native_dsp/lv2_host.c:373`,
`native_dsp/pipewire_engine.c:280-330`,
`dsp/manager.py:1007-1276` (Compiler, u. a. `:1121` pairs, `:1157` param path,
`:1162-1163` Delay, `:1174-1176` Upmixer mode/fft, `:1195` Loudness fft,
`:1228` Limiter lk, `:1255` output), `dsp/runtime.py:1174`/`1232`/`1247`/`1299`
(swap config, Hot-Update, Shape/Value, Live-Txn),
`native_src/build.py:133`, `native_src/resample-worker.c:82-91`,
`native_surround/{README.md,surround.h:9-14,surround.c:386-410,ingress.c:100-127}`,
`static/measurement_dsp.js:180-345`,
`static/index.html:1211-1233` + `static/app.js` (`syncMeasurementCalibrationMenu`),
`MANUAL.md:393`/`415-423`, `main.py:5258-5270`.

## 15. Implementierung: AV Sync

- Der Native-DSP-Engine bilanziert die tatsächliche Kette pro Quelle und Ausgang
  und veröffentlicht kohärente, atomare `latency get`-Snapshots. Die Bilanz folgt
  Live-Kontrollen, Bypass, Rebuilds, Rate und Eingangsguthaben; Loudness verwendet
  die FFT-Größe statt des veralteten LV2-Ports.
- Schema v6 speichert `av_sync` mit Ziel und Quellen-/Ausgangsbasis pro Modus.
  Die Basis enthält `input + dsp + balance`, nicht Alignment, bewusste Delays
  oder den gemeinsamen AV-Rest. Eine neue Teilkalibrierung verschiebt erhaltene
  und inaktive Basiswerte gemeinsam in dieselbe Epoche. Eine gemeinsame
  Normalisierung verhindert negative gespeicherte Millisekunden und erhält die
  relative Zeit, nicht zwingend jeden alten Zahlenwert.
- Surround-Quellen werden **vor** gemischten Bass-/Fold-down-Sends ausgeglichen;
  danach folgt die Ausgangsbilanz. Quelle, Ausgangsausgleich, Alignment und
  gemeinsamer Rest teilen sich das 500-ms-Budget jedes betroffenen Pfads.
  Bewusste unterschiedliche Delays bleiben Absicht; unvereinbare gemischte Pfade
  werden als `mixed` gemeldet.
- Bestandskalibrierungen übernehmen erst nach verifiziertem normalem Rendering
  dessen aktuelle Basis, ohne die vorhandene Spreizung ein zweites Mal
  auszugleichen. Das ist eine sichere Adoption, kein Nachweis der historischen
  Kalibrierbedingungen. Neu vermessen erzeugt eine gemessene Basis.
- Neue Korrektur-WAVs tragen validierte RIFF-`fxlt`-Metadaten pro Kanal für
  algorithmische und beabsichtigte Frames. Nicht markierte IRs gelten als
  unbekannt; ihre Länge wird nicht als `N/2` interpretiert. `balance_unavailable`
  deaktiviert den relativen Ausgleich ausdrücklich und verhindert, dass eine
  unbekannte IR als vermeintliche Absenkung auf Null behandelt wird.
- Speaker Align und AutoSub übernehmen nur verifizierte Sweep-Zeitevidenz;
  AutoSub verwendet den Raw-Sweep, nicht das danach restaurierte DSP-Rendering.
  Laufende Messungen prüfen auch kleine Timingänderungen sowie Nebenquellen
  gemischter Sends vor dem erfolgreichen Abschluss.
- Die UI unter **Calibration → AV Sync** zeigt Ziel, erreichbare Gesamtzahl,
  Grenzen und Einzelanteile in allen drei Modi. Unerreichbare Ziele bleiben
  gespeichert, werden aber nicht als angewandt ausgegeben. Gerätepuffer,
  Schallflug und variable SRC-Reserve zählen nicht als deterministisches
  Guthaben. Es gibt keinen Video-Offset.

Reproduzierbare native Prüfung: `sh native_dsp/test_av_sync.sh`. Sie prüft
Snapshots und echte Impulspositionen bei 44,1/48/96 kHz, Stereo/Sub,
Loudness-Livewechsel, Direct, Eingangsguthaben und Surround-Recompile nach einer
Teilkalibrierung. `test_upmix_hq.c` prüft außerdem alle acht Kanäle sample-genau
für FFT 8192. Den Upmixer-Bypass für Mehrkanalmaterial prüfen `test_upmix_fast.c`
und `test_upmix_hq.c` (Kanalzahl, direkter Pfad, Latenz, Wechsel in beide
Richtungen) sowie `test_upmixer_delay_follows_the_programme` in
`test_av_sync.c` (Readback und Impulsposition, mit und ohne AV-Sync-Ziel). Diese elektronischen Prüfungen ersetzen keine neue akustische
Abnahme am Hörplatz und keine physische Messung der DAC-/TV-Latenz.
