# FXRoute — Compare/Combine UI variants

## Variante A — Separate Combine Card

**Struktur / Wireframe**

```text
[ Preset Library / Browser ]
[ Current Preset / Actions ]
[ A/B Compare ]
  [A preset] [swap] [B preset] [Compare action]

[ Combine presets ]
  Source presets: [Preset 1] [Preset 2] [+ Add]
  Order:          [1] [2] [3]  (drag / move)
  Result name:    [ New combined preset ]
  Actions:        [Create combined preset]
```

**Warum gut**
- Compare bleibt oben und klar dominant.
- Combine ist verständlich als eigener, seltener Workflow.
- Reihenfolge und Mehrfachauswahl lassen sich ohne Enge zeigen.
- Passt gut zu der Tendenz, bei 4 Hauptblöcken zu bleiben.

**Nachteile / Risiken**
- Braucht etwas mehr vertikalen Platz.
- Wirkt nur dann sauber, wenn die Card sichtbar einfacher bleibt als Compare.
- Auf kleinen Heights könnte Combine schnell „unter dem Fold“ landen.

**Empfehlung für v1**
- **Bevorzugt für v1.**
- Am klarsten, risikoarm und am nächsten am bestehenden FXRoute-Aufbau.

**Mobile-Hinweis**
- Untereinander stapeln.
- Combine standardmäßig eingeklappt oder erst unter Compare zeigen.

---

## Variante B — Compare Card mit Secondary Expand

**Struktur / Wireframe**

```text
[ A/B Compare ]
  [A preset] [swap] [B preset] [Compare action]
  Secondary row: [Combine presets ▾]

  when expanded:
    Sources: [Preset 1] [Preset 2] [+ Add]
    Order:   [1] [2] [3]
    Result:  [ New combined preset ] [Create]
```

**Warum gut**
- Sehr kompakt.
- Combine bleibt klar sekundär.
- Wenig Eingriff in die bestehende Seitenstruktur.

**Nachteile / Risiken**
- Höheres Risiko, den Compare-Bereich zu überladen.
- Nutzer könnten Compare und Combine als zu eng gekoppelt lesen.
- Bei mehreren Source-Presets wird die Expand-Fläche schnell unruhig.

**Empfehlung für v1**
- **Nur zweite Wahl.**
- Sinnvoll, wenn Platz extrem knapp ist und die Expansion sehr reduziert bleibt.

**Mobile-Hinweis**
- Nur als Accordion sinnvoll.
- Expanded State muss sauber begrenzt sein, sonst wird der Bereich zu lang.

---

## Gesamt-Empfehlung

Für **v1 klar Variante A**: Compare bleibt schnell und dominant, Combine wird sauber ergänzt statt hineingedrückt.

Wenn später mehr Platzdruck oder stärkere Verdichtung nötig ist, kann Variante B als kompaktere Iteration geprüft werden.