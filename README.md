# Light Buddy

Custom Integration für Home Assistant: **ein Licht pro Raum**, ohne Lichtgruppen pflegen zu müssen.

Für jeden Bereich (Area) mit mindestens einer Lampe legt Light Buddy eine Licht-Entität an, z. B. `light.all_wohnzimmer` mit dem Namen „Alle Lichter in Wohnzimmer“ (auf Englisch „All lights in Wohnzimmer“). Sie schaltet und dimmt alle Lampen des Bereichs gemeinsam und funktioniert in einer normalen Dashboard-Kachel mit Helligkeitsregler.

Dazu kommen ein Farbtemperatur-Offset pro Lampe und ein Zeitplan für die Farbtemperatur. Geplant: Disco-Modus.

## Voraussetzungen

Home Assistant 2026.10 oder neuer.

## Installation

**HACS:** HACS → Integrationen → Menü → Benutzerdefinierte Repositories → URL dieses Repositorys mit Typ „Integration“ hinzufügen. Danach „Light Buddy“ installieren und Home Assistant neu starten.

**Manuell:** Den Ordner `custom_components/light_buddy` in den Ordner `custom_components` deiner Home-Assistant-Konfiguration kopieren und Home Assistant neu starten.

## Einrichtung

Einstellungen → Geräte & Dienste → Integration hinzufügen → **Light Buddy**. Es gibt nichts weiter einzustellen; die Räume entstehen automatisch.

## Welche Lampen gehören zu einem Raum?

- Lampen, deren Entität dem Bereich zugeordnet ist.
- Lampen ohne eigenen Bereich, deren Gerät im Bereich liegt.
- Nicht dabei sind deaktivierte Entitäten, Lichtgruppen (`group`) und die Light-Buddy-Entitäten selbst.

Wenn du eine Lampe verschiebst, einen Bereich anlegst oder umbenennst, passt sich Light Buddy ohne Neustart an. Bereiche ohne Lampen verlieren ihr Raum-Licht.

## Optionen

Einstellungen → Geräte & Dienste → Light Buddy → **Konfigurieren**:

- **Ausgeschlossene Bereiche**: Für diese Bereiche gibt es kein Raum-Licht.
- **Ausgeschlossene Lampen**: Diese Lampen gehören zu keinem Raum, z. B. Template-Lichter, die selbst andere Lampen steuern.

## Farbtemperatur-Offset

Für eine Lampe, die kälter oder wärmer wirkt als die anderen. Jede Lampe mit Farbtemperatur in einem Raum hat auf ihrer Geräteseite unter **Konfiguration** zwei Einstellungen:

- **Farbtemperatur-Offset**: Schieberegler für den Offset.
- **Einheit Farbtemperatur-Offset**: Kelvin oder Mired. Beim Umschalten wird der Offset so umgerechnet, dass die Lampe bei 3000 K gleich aussieht.

**Kelvin oder Mired?** Ein Kelvin-Offset wirkt bei warmem Licht stark und bei kaltem kaum: -300 K sind bei 2700 K deutlich, bei 6000 K fast unsichtbar. Ein Mired-Offset wirkt überall gleich stark, so wie das Auge Farbtemperatur wahrnimmt. In Kelvin macht ein negativer Wert die Lampe wärmer, in Mired ein positiver.

**Ausprobieren:** Schalte den Raum ein. Jede Änderung am Schieberegler wirkt sofort auf die Lampe.

1. Raum auf einen warmen Wert stellen, z. B. 2700 K, und den Offset anpassen, bis die Lampe zu den anderen passt.
2. Raum auf einen kalten Wert stellen, z. B. 5000 K. Passt die Lampe noch, ist die Einheit richtig. Sonst die Einheit umschalten und wiederholen.

Weitere Hinweise:

- Der Offset wirkt nur, wenn die Farbtemperatur über ein Raum-Licht gesetzt wird. Steuerst du die Lampe direkt, bekommt sie den Wert ohne Offset.
- Der Zielwert wird auf den Bereich der Lampe begrenzt.
- Das Raum-Licht zeigt den gewünschten Wert an, nicht den verschobenen.
- Die Einstellungen erscheinen nicht auf Dashboards und werden von Bereichs-Aktionen nicht erfasst.
- Lampen, die nur Farben können, haben keinen Offset.

## Zeitplan

Jeder Raum folgt einem Zeitplan für die Farbtemperatur. Zwischen den Zeitpunkten wird gleitend übergeblendet, auch über Mitternacht. Die Kurve folgt dem Tageslicht und ist mittags am kältesten:

| Uhrzeit | Farbtemperatur |
|---|---|
| 05:30 | 2200 K |
| 06:30 | 3000 K |
| 08:00 | 4000 K |
| 12:30 | 5000 K |
| 16:00 | 4500 K |
| 18:30 | 3500 K |
| 21:00 | 2700 K |
| 22:30 | 2200 K |

Pro Raum gibt es:

- **Zeitplan** (`switch.schedule_<bereich>`): schaltet den Zeitplan für den Raum ein oder aus.
- **Ziel-Farbtemperatur** (`sensor.target_color_temperature_<bereich>`): der aktuelle Zielwert, z. B. für eigene Automationen.

So verhält sich der Zeitplan:

- **Einschalten über das Raum-Licht:** Ausgeschaltete Lampen gehen direkt mit dem Zielwert an, ohne vorher die alte Farbe zu zeigen. Mit einer eigenen Farbe (z. B. aus einer Szene) bleibt die Farbe unverändert.
- **Einschalten an der Lampe selbst oder per Wandschalter:** Die Lampe bekommt den Zielwert innerhalb von etwa einer Sekunde.
- **Laufende Anpassung:** Jede Minute bekommen eingeschaltete Lampen den Zielwert mit einem kurzen Übergang von 2 s. Gesendet wird nur, wenn die Lampe mindestens 50 K davon entfernt ist. Die Räume sind dabei über die Minute verteilt.
- **Farbige Lampen:** Lampen, die gerade eine Farbe zeigen, lässt der Zeitplan in Ruhe.
- **Offsets:** Farbtemperatur-Offsets gelten auch für den Zeitplan.

## Verhalten

- **Zustand:** Der Raum ist an, sobald eine Lampe an ist. Helligkeit und Farbtemperatur sind der Mittelwert der eingeschalteten Lampen.
- **Farben:** Farben gehen nur an Lampen, die Farbe können. Weiße Lampen bekommen dabei nur die Helligkeit.
- **Zigbee-Netz:**
  - Lampen mit gleichen Werten werden in einem Aufruf zusammengefasst.
  - Pro Raum geht höchstens alle 0,3 s ein Befehl raus. Beim Ziehen am Regler wird nur der letzte Wert gesendet.
  - Ausschalten wird nur an Lampen geschickt, die nicht schon aus sind.
- **Bereich:** Die Light-Buddy-Entität ist bewusst keinem Bereich zugeordnet. Sonst würden Bereichs-Aktionen wie „Alle Lichter im Wohnzimmer aus“ jede Lampe doppelt ansteuern.

## Entwicklung

```sh
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest
.venv/bin/ruff check .
```
