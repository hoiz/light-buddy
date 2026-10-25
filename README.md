# Light Buddy

Custom Integration für Home Assistant: **ein Licht pro Raum**, ohne Lichtgruppen pflegen zu müssen.

Für jeden Bereich (Area) mit mindestens einer Lampe legt Light Buddy eine Licht-Entität an, z. B. `light.all_wohnzimmer` mit dem Namen „Alle Lichter in Wohnzimmer“ (auf Englisch „All lights in Wohnzimmer“). Sie schaltet und dimmt alle Lampen des Bereichs gemeinsam und funktioniert in einer normalen Dashboard-Kachel mit Helligkeitsregler.

Geplant: Farbtemperatur-Offset pro Lampe, Farbtemperatur nach Tageszeit, Disco-Modus.

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
