[README.txt](https://github.com/user-attachments/files/33031159/README.txt)
SPEED DYNAMICS GT7 – V2

Enthalten:
- SpeedDynamicsGT7.py: GT7 UDP Bridge
- web/index.html: Dashboard

Wichtig:
- Python benoetigt pycryptodome.
- Die Dashboard-Vorschau ist selbststaendig und enthaelt eine eingebettete GT3-Silhouette, damit keine Bilddateien in der Vorschau fehlen.
- Die Live-Version erwartet die Webdatei unter web/index.html.

Delta:
- Referenz ist eine voll aufgezeichnete Runde.
- Vergleich erfolgt anhand der kumulierten Weltposition/Distanz, nicht gegen die komplette Best-Lap-Zeit.
- Teilrunden werden nicht als Referenz akzeptiert.

Fuel:
- GT7 liefert den Fuel-Level in Litern.
- Rundenreichweite wird aus dem gemessenen Verbrauch abgeschlossener Runden geschaetzt.
