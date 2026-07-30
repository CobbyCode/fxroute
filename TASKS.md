# TASKS — FXRoute

## Goal

Den bestehenden sweepbasierten AutoSub-Pfad auf einen Auto-Gain-Suchbereich
von −6 bis +6 dB erweitern und jeden Gain-Sweep anhand der tatsächlichen
vierkanaligen Stage-Ausgangspeaks sicher auf höchstens −1 dBFS begrenzen.

## Tasks

- [x] ASG-001 — Implementiert, auf `.104` gebaut/deployt und mit realem Vierkanal-Peakvergleich verifiziert
- [x] LDN-001 — LSP-Loudness, exklusives Auto Gain, Volume-Routing und SPL-Kalibrierung implementiert und getestet
- [x] LDN-002 — Live-Follow-up: autoritatives Volume-Mapping, Kalibrierungserhalt und HTTP-400-Mutex
- [x] LDN-003 — UMIK-1-Metadaten, Cal-Zuordnung, Capture-Gain und automatische SPL-Ermittlung
- [x] LDN-006 — Profilbezogene SPL-Kalibrierung sicher an den aktiven Loudness-Block gekoppelt
- [x] LDN-007 — UMIK-SPL-Referenz korrigiert und unveröffentlichten Trim-Migrationspfad entfernt
- [x] SR-001 — Gespeicherte Default-Samplerate bei Start und nach Messfenstern zuverlässig wiederherstellen
- [x] LDN-008 — Persistente Loudness Strength mit pegelneutraler Offset-Kopplung
- [x] LDN-009 — Strength-Offsets 0/6/12/18 dB und kompaktes Responsive-Layout
- [x] LDN-010 — Strength-Offsets 0/10/20/30 dB bei unveränderter pegelneutraler Kopplung
- [ ] LDN-011 — Verworfen: kein feststellbarer Nutzen, anschließend kein Ton aus den Mains
- [x] LDN-012 — Direkter Strength-Laufzeitpfad ohne Preset-/Graph-Reload
- [x] LDN-013 — Unveränderten nachlaufenden Extras-Save ohne Nebenwirkungen ignorieren
- [x] LDN-014 — Strength-Wechsel durch temporäre Loudness-Ausgangsabsenkung absichern
- [x] LDN-015 — Neutralen Loudness-Laufzeitzustand nur für SPL Calibration verwenden
- [x] LDN-016 — Kanonisches Volume beim Strength-Wechsel erhalten
- [x] LDN-017 — Auto Gain und Loudness gemeinsam mit gekoppeltem Arbeitspunkt betreiben
- [x] LDN-018 — Gemeinsame Auto-Gain-/Loudness-Runtime-Übergänge stabilisieren
- [x] LDN-019 — Reale Strength-Save-Acknowledgement- und Transientenfehler beheben
- [x] LDN-020 — Verbleibende Loudness-Disable- und Strength-Transienten behoben und akustisch bestätigt
- [ ] LDN-021 — Numerische Loudness-Strength 1–10 umgesetzt und auf `.104` im Review
- [ ] LDN-022 — Auto Gain und Loudness nur während SPL Calibration neutralisieren

## Historical Predictive AutoSub Tasks

- [x] PAS-001 — Bestandsaufnahme, Implementierungsplan, Umsetzung und automatisierte Tests
- [x] PAS-002 — Main-Review von Diff, Tests, Modussemantik und Hardwaretest-Risiken
- [x] PAS-003 — Gesichertes Testdeployment; bei 2.1-Verification sicher abgebrochen
- [x] PAS-004 — 2.1-Abweichung isoliert, Invarianten/Diagnostik ergänzt und Drei-Punkt-Hardwarevergleich restauriert abgeschlossen
- [x] PAS-005 — Reference Mode des PAS-004-Laufs forensisch pro Sweep belegt
- [x] PAS-006 — 2.1 Electrical-Predictive praktisch diagnostiziert, Datenpfad korrigiert und technische Reproduzierbarkeitsgrenze nachgewiesen
- [ ] PAS-007 — Sicher abgebrochen: hörbares Crackling rechts / möglicher Doppelpfad; Messungen ungültig
- [x] PAS-008 — Transienten EasyEffects→Hardware-Doppelpfad beim Streamstart live belegt; neue PAS-007-Captures bleiben gesperrt
- [ ] PAS-009 — Fehlgeschlagen: Gate erkennt rekonstruierte EE→Hardware-Direktlinks nach Audiofreigabe, verhindert sie aber nicht präventiv
- [ ] PAS-010 — Fehlgeschlagen: EasyEffects als Erzeuger belegt; eng begrenzte ACL-Varianten verhindern den Direktpfad nicht lebenszyklusstabil
- [ ] PAS-011 — Wieder geöffnet: direkter 2.1-MSO-Ansatz einmalig über den bestehenden Produktionspfad mit realem Laufzeitgate
- [ ] PAS-012 — Neuer End-to-End-Nachweis mit Main L/R, Sub 1/2, prädiktiver Kandidatensuche und wenigen Kontrollmessungen
- [x] PAS-013 — Vier branchenspezifisch gültige Komponenten und drei reale Kontrollen; Vorhersagerangfolge widerlegt Ersatz des Suchlaufs

## Notes

- Aktueller Stand: ASG-001 sowie LDN-001/002/003/006/007 abgeschlossen.
- ASG-001 wurde auf `.104` gebaut/deployt und mit realem Vierkanal-
  Peakvergleich bestanden. Ein späterer Nutzerlauf ergab berechnet
  `+1,964 dB` und wurde nur in der UI als `+2,0 dB` dargestellt; der aktive
  Suchbereich war nachweislich `[-6,+6] dB`, `clamped=false`.
- Loudness/SPL letzter Commit:
  `c0003a4c6f1a703d8c244b981aca7a65c5038e8d`; auf `.104` deployt und live
  bestätigt.
- Loudness Strength Commit
  `07dfbb464e4b956fb1fb3dcaf42c495bbbc9e6ca` ist auf `.104` deployt und live
  bestätigt. Full/Med/Light/Min halten die Summe aus LSP Volume und
  Loudness Output Gain exakt auf dem bestehenden Masterwert A; Strength blieb
  nach Service-Neustart erhalten.
- LDN-009 Commit `9b1c0d6f85b2803d2f608d7e0dd7a15c9ae19bf7`
  ist auf `.104` deployt. Strength verwendet `0/+6/+12/+18 dB`; die kompakte
  Loudness-Zeile zeigt kein sichtbares Strength-Label mehr, verwendet
  `aria-label="Strength"` und kürzt `FFT Size` zu `FFT`. Formel,
  Pegelneutralität, Persistenz, IDs und Backend bleiben unverändert.
- LDN-010 Commit `4fbdcc027c64757b86c4f0c6f72628473b6d25e2`
  ist auf `.104` deployt. Strength verwendet nun `0/+10/+20/+30 dB`.
  Bei A = −21,39283941 dB blieb die Summe für alle vier Stufen exakt A;
  Min blieb über den Service-Neustart erhalten.
- UMIK-dBFS→SPL-Referenz von `+120 dB` auf `+124 dB` korrigiert.
  Automatische Live-Messung: 66,21 dB SPL gegenüber 65,7 dB in REW
  (+0,51 dB).
- Unnötiger `calibrationTrimDb`-Migrations-/Rewritepfad vollständig entfernt.
  Loudness-Gainstruktur unverändert und live bestätigt.
- UI-Stand auf `.104`: Loudness wurde ausschließlich in
  `static/index.html` aus `Protection` nach `Tone` verschoben und steht dort
  direkt vor `Bass enhancer`. IDs, JavaScript, Zustandslogik, Persistenz,
  Auto-Gain-Mutex und Backend sind unverändert. Die UI-Umsortierung ist lokal
  noch nicht committet.
- Live-Zustand nach LDN-010-Deployment: FXRoute aktiv, Loudness an,
  Strength `Min`, FFT 4096; bestehender Nutzerzustand nicht verändert.
  Kein Push oder Release.
- LDN-011 wurde nach Nutzerprüfung verworfen: keine feststellbare Verbesserung,
  anschließend kein Ton aus den Mains. `.104` wurde aus dem unmittelbaren
  Pre-LDN-011-Backup restauriert; Laufzustand wieder `-7dB`, Volume 38,
  Loudness aus, Strength Min, FFT 8192. Bestätigter Code-Stand bleibt LDN-010
  (`4fbdcc027c64757b86c4f0c6f72628473b6d25e2`). Kein Push oder Release.
- LDN-012 ist freigegeben: Strength soll die beiden aktiven Loudness-Parameter
  direkt und richtungssicher ändern, anschließend genau einmal persistieren
  und dabei keinen EasyEffects-Preset-/Graph-Reload auslösen.
- LDN-012 ist auf `.104` deployt und technisch live bestätigt. Alle
  benachbarten Übergänge in beide Richtungen hielten die Endsumme exakt bei
  `-30.5182983699436 dB`; Bypass, Limiter, System-Master und alle Main-/Sub-
  Links blieben unverändert. Kein Preset-/Graph-Reload und kein
  Peak-Monitor-Refresh.
- SR-001 abgeschlossen; Commit
  `fc406f0c756e64718a8e25235f9d14190edf1651` auf `.104` deployt.
  Bestätigter Live-Lifecycle: normal 44,1 kHz, Measurement-Session 48 kHz,
  Fenster schließen zurück auf 44,1 kHz. Kein Push oder Release.
- Das kurze Mute-Fenster um den vorhandenen Ratewechsel wurde live in beide
  Richtungen erprobt und verworfen: Knackser blieben hörbar. Der bestätigte
  Stand bleibt `fc406f0c756e64718a8e25235f9d14190edf1651`.
- Ausschließlich bestehender sweepbasierter AutoSub-Pfad; keine
  Predictive-AutoSub-Arbeit und keine breite Architekturänderung.
- Der bestehende zweikanalige EasyEffects-Peak-Monitor bleibt unverändert.
- Kein Push, Release oder Deployment ohne gesonderte Freigabe.
- Die nachfolgenden PAS-Notizen sind historischer Kontext.
- **Verbindlicher Neustartpunkt; mit PAS-011 zur Ausführung freigegeben:**
- **Aktueller Neustartpunkt: PAS-012.** Der Nutzer hat den bisherigen Stopp
  ausdrücklich aufgehoben und einen neuen, zielgerichteten End-to-End-Versuch
  freigegeben.
- **Aktueller Ausführungspunkt: PAS-013.** PAS-012 ist nur Zwischenstand. Der
  reale EasyEffects→Hardware-Doppelpfad muss zielgerichtet diagnostiziert und
  robust behoben werden; Routing-, EasyEffects- und Messablauf-Code dürfen
  dafür geändert werden. Danach ist ohne künstlichen Zwischenstopp der volle
  Mess- und Validierungslauf auszuführen.
- PAS-013 abgeschlossen: Main L/R und Sub 1/2 sind real gültig, aber die
  drei Kontrollen zeigen 5,27–6,33 dB Form-RMSE und eine invertierte
  Best-/Gegenkandidaten-Rangfolge. Branchenspezifische post-Crossover-ER
  erhält keine gemeinsame Main/Sub-Phase; eine konstante A/B-Korrektur
  erklärt die Abweichung nicht. Der reale AutoSub-Suchlauf ist so nicht
  ersetzbar. Produktion vollständig restauriert.
- PAS-012 verwendet vier direkt und einzeln mit konsistenter Electrical
  Reference aufgenommene Komponenten: Main Left, Main Right, Sub 1 und Sub 2.
  Daraus werden offline komplexe Summen für Delay- und Polaritätskandidaten
  berechnet und nur wenige ausgewählte Kandidaten real kontrolliert.
- Keine reguläre AutoSub-Integration, kein neues Worktree, keine Projektkopie,
  kein großes Framework und keine neue Routing-/EasyEffects-Architektur.
- **Historischer Neustartpunkt für PAS-011:**
  - bisherigen Main+Sub-minus-Main-Provider nicht weiterverfolgen;
  - Main-only direkt mit Electrical Reference erfassen;
  - Sub-only direkt mit derselben Electrical Reference erfassen;
  - keine separate akustische Ausrichtung;
  - für beide Komponenten identische Verarbeitung und Fensterung relativ zum
    ER-Anker verwenden;
  - Main und Sub komplex mit Delay und Gain addieren;
  - zunächst ausschließlich `d0−1 ms`, `d0` und `d0+1 ms` gegen echte
    Main+Sub-Sweeps prüfen;
  - ausschließlich 2.1;
  - keine Routing-/EasyEffects-Grundsatzarbeiten;
  - vorhandenes verifiziertes Backup nur wiederverwenden, wenn der Stand
    unverändert und dies vor Nutzung frisch verifiziert ist.
- PAS-011 ist das einzige freigegebene Ticket. Bei einem konkreten Blocker
  dokumentiert stoppen und keine neue Ticketkette eröffnen.
- PAS-011: Backup/Baseline erneut vollständig verifiziert. Keine
  Implementierung oder Hardwaremessung, weil PAS-010s nicht präventiv
  beherrschter EasyEffects→Hardware-Direktlink das verpflichtende
  Doppelpfad-Abbruchgate blockiert; neue Routing-/EasyEffects-Grundsatzarbeit
  war ausdrücklich ausgeschlossen. Direkter Ansatz bleibt ungeprüft.
- PAS-011-Reopen: Der frühere theoretische Abbruch ist kein Ergebnis.
  Historische Direktlink-Möglichkeit allein blockiert nicht mehr. Zuerst einen
  kurzen normalen 2.1-Kontrollsweep ausführen; bei hörbar sauberem Lauf genau
  einen Implementierungs- und Hardwaredurchgang. Nur bei tatsächlich
  beobachtetem Crackeln, Doppelwiedergabe, zwei audiotragenden Pfaden,
  fehlgeschlagenem Sweep oder ungültiger Electrical Reference abbrechen.
- Bestehendes AutoSub-Verhalten ohne Electrical Reference muss unverändert bleiben.
- Kein Push, Release, Deployment oder Hardwareeingriff.
- PAS-003: keine Hardwarefreigabe; 2.1-Prediction-Abweichung muss vor einem
  erneuten Mehrpunktlauf isoliert und detailliert persistiert werden.
- PAS-004: Capture-Zustand und Scorekette korrigiert; d0 und ±1 ms bleiben
  real deutlich außerhalb der Limits. Nächster Forschungsgegenstand ist die
  Wiederholbarkeit/gemeinsame Zeitbasis getrennter ER-Captures.
- PAS-005: alle acht PAS-004-Sweeps waren nachweislich echte Electrical
  Reference ohne Fallback; keine Gate-/Code-/Hardwareänderung erforderlich.
- PAS-006: eigenständig geplanter, begrenzter 2.1-Diagnose- und Hardwarelauf;
  mehrere reale Delay-Punkte plus unabhängige d0-Wiederholungen. Aktuelle
  Form nicht zuverlässig: ER-Anker stabil, aber akustisch-komplexe Antwort
  beziehungsweise Direktschall-/Fensterwahl captureübergreifend variabel.
  Vollständiges Backup/Restore, keine dauerhafte Kandidatenübernahme.
- PAS-007: mindestens vier unabhängige Main+Sub-Captures bei unverändertem
  2.1-DSP-Zustand; persistente Zwischenstände und Offline-Varianten A–D.
  Sicher abgebrochen, bevor die Messreihe zustande kam: rechts hörbares
  Crackling beziehungsweise möglicher Doppelpfad. Zwei bereits abgeschlossene
  Sweeps sind ungültig und dürfen nicht ausgewertet werden. `.104` restauriert.
- PAS-008: vor jeder weiteren Predictive-/Fensterdiagnose den Wiedergabepfad
  prüfen. PAS-007/PAS-006-Logs und temporären Code auswerten, anschließend nur
  kurzer Links/Stop/Rechts-Check mit Stream-, PipeWire-, Prozess- und
  XRun-Nachweisen. Keine neuen ER-Captures oder Kandidatenläufe.
- PAS-009: zentraler synchroner Link-Preflight vor dem ersten Audioblock,
  ER-Fehler ohne impliziten Monitor-Zweitlauf und zehnfache Links/Stop-
  Verifikation. Danach ausschließlich kurzer hörbarer Links/Stop/Rechts-Test;
  vollständiger Restore, kein Predictive-/PAS-007-/2.2-Lauf. Review nicht
  angenommen: Beim ersten hörbaren linken Lauf erschienen nach Release erneut
  direkte EE→Hardware-Links (IDs 132/177). Sofort abgebrochen, rechts nicht
  gestartet, `.104` vollständig restauriert. Nächster Schritt muss die
  WirePlumber/EasyEffects-Rekonstruktion präventiv unterbinden.
- PAS-010: zuerst einen kontrollierten Streamstart ohne Codeänderung
  vollständig forensisch erfassen und Knotentyp/Policy nachweisen. Danach nur
  die kleinste auf FXRoute beziehungsweise den konkret beteiligten
  EasyEffects-Knoten begrenzte präventive Lösung umsetzen. Globale Policy-,
  Default-Sink- oder pauschale EasyEffects-Änderungen sind ausgeschlossen.
  Fail-closed bei fehlendem/mehrdeutigem Helper oder jedem Direktlink. Erst
  nach automatisierten Tests 10×L/10×R und einen begrenzten Hörtest; keine
  PAS-007-, Predictive- oder 2.2-Läufe.
  Ergebnis: EasyEffects Client 86 erzeugte die Direktlinks selbst. Nur das
  Linkrecht am Hardwareziel zu entziehen war wirkungslos; vollständiges
  Ausblenden der Hardware für EasyEffects war nach zwei linken Stummzyklen
  lebenszyklusinstabil und erzeugte beim Cleanup erneut Direktlinkereignisse.
  Fail-closed vor L03-Freigabe; rechts/Hörtest nicht gestartet; `.104`
  restauriert.
