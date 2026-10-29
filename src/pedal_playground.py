#!/usr/bin/env python3
"""Pedal-Spielwiese: Stream Deck Pedal -> Aktionen, live im Terminal visualisiert.

Bildet die Bedienung von ``piper-record`` (sfg_piper_lerobot) ohne Roboter nach:
gleiche Tasten-Flags wie ``RecordKeys`` und gleiche Zustandsmaschine
READY -> RECORDING -> DECISION -> READY. So laesst sich die Bedienung lernen,
bevor das grosse Repo angefasst wird.

Modi:
  (Standard)  Pedal-Callback fuettert die Flags direkt - so wuerde ein
              Pedal-Backend in SessionKeys arbeiten.
  --uinput    Pedal wird zur virtuellen Linux-Tastatur (/dev/uinput) und
              tippt echte Tasten (SPACE/S/D). Die Spielwiese reagiert dann nur
              noch auf das, was im Terminal ankommt - genau wie piper-record
              heute. Braucht python-evdev und Schreibrecht auf /dev/uinput.

Pedal-Belegung: mitte = SPACE (Start/Stopp/Speichern), links = S (Speichern),
rechts = D (Verwerfen) - siehe PEDAL_KEYS.

Tasten im Terminal: Leertaste/Enter, s, d, q/Esc wie in piper-record;
1/2/3 simulieren die drei Pedale; Ctrl+C beendet sofort.
"""

import argparse
import os
import queue
import select
import sys
import termios
import time
import tty
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # StreamDeck aus diesem Repo

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

FPS = 30  # Takt der Aufnahmeschleife in piper-record; hier zaehlt er die "Frames"
SIM_PRESS_S = 0.15  # Wie lange ein per 1/2/3 simuliertes Pedal "gedrueckt" aussieht

# DIE zentrale Belegung: Pedal-Index (so meldet ihn die Bibliothek) -> Taste.
# Das Pedal kennt keine Tasten, nur Index 0/1/2 - erst diese Tabelle gibt ihnen
# eine Bedeutung. Die Tastennamen entsprechen denen in sessions/keys.py.
#   mitte  = Haupttaste (SPACE): Start -> Stopp -> Speichern
#   links  = S: Speichern (nur in DECISION wirksam)
#   rechts = D: Verwerfen (nur in DECISION wirksam)
PEDAL_KEYS = {0: "s", 1: "space", 2: "d"}
PEDAL_NAMES = {0: "LINKS", 1: "MITTE", 2: "RECHTS"}

# Was eine Taste in welcher Phase bewirkt - reine Anzeige fuer die Kaesten,
# die echte Logik steckt in RecordSession.tick()
KEY_EFFECT = {
    "READY": {"space": "Aufnahme starten", "s": "-", "d": "-"},
    "RECORDING": {"space": "Aufnahme beenden", "s": "-", "d": "-"},
    "DECISION": {"space": "Speichern", "s": "Speichern", "d": "Verwerfen"},
}
PHASE_COLOR = {"READY": "33", "RECORDING": "31", "DECISION": "36", "ENDE": "90"}


# ---------------------------------------------------------------------------
# Logik (1:1 nachgebaut aus sfg_piper_lerobot)
# ---------------------------------------------------------------------------


class RecordFlags:
    """Nachbau von sessions/keys.py:RecordKeys - Tasten setzen nur Flags.

    Wichtiges Prinzip: Eine Taste LOEST NICHTS DIREKT AUS. Sie hebt nur eine
    Fahne. Die Aufnahmeschleife schaut 30x pro Sekunde nach und entscheidet
    je nach Phase, ob die Fahne etwas bedeutet. Darum ist es egal, woher die
    Taste kommt (Tastatur, Terminal, Pedal) - alle rufen nur feed() auf."""

    def __init__(self):
        self.episode_end = False
        self.save_requested = False
        self.discard_requested = False
        self.stop_requested = False

    def feed(self, key):
        if key in ("space", "enter"):
            self.episode_end = True
        elif key == "s":
            self.save_requested = True
        elif key == "d":
            self.discard_requested = True
        elif key in ("q", "esc"):
            self.stop_requested = True


class RecordSession:
    """Nachbau der Zustandsmaschine aus record_real.py:run_record (ohne Roboter)."""

    def __init__(self, log):
        self.phase = "READY"
        self.frames = 0
        self.attempt_start = 0.0
        self.saved = []  # Frames je gespeicherter Episode
        self.discarded = 0
        self.log = log

    def tick(self, f: RecordFlags):
        """Ein Takt der Schleife: Fahnen lesen, ggf. Phase wechseln, Fahnen senken.

        Fahnen, die in der aktuellen Phase nichts bedeuten (z.B. S in READY),
        bleiben stehen, werden aber beim naechsten Phasenwechsel geloescht -
        so speichert ein alter, versehentlicher Tritt nie spaeter automatisch."""
        # READY: Arm ist live, nichts wird aufgenommen. Nur SPACE (Start) und q zaehlen.
        if self.phase == "READY":
            if f.stop_requested:
                self._end("Session beendet")
            elif f.episode_end:
                f.episode_end = False
                f.save_requested = False  # alte Druecker speichern nicht spaeter automatisch
                f.discard_requested = False
                self.frames = 0
                self.attempt_start = time.monotonic()
                self.phase = "RECORDING"
                self.log(f"RECORDING: Versuch laeuft (Episode {len(self.saved)} falls gespeichert)")
        # RECORDING: Frames werden gepuffert (noch nicht auf Platte). SPACE beendet
        # den Versuch, q verwirft ihn. S/D wirken hier bewusst NICHT.
        elif self.phase == "RECORDING":
            if f.episode_end:
                f.episode_end = False
                f.save_requested = False
                f.discard_requested = False
                self.phase = "DECISION"
                self.log(f"DECISION: {self.frames} Frames gepuffert, NOCH NICHT gespeichert")
            elif f.stop_requested:
                self.discarded += 1
                self._end("Stopp waehrend Aufnahme -> Versuch verworfen, Session beendet")
        # DECISION: Versuch ist zu Ende, liegt aber nur im Puffer. Erst hier
        # entscheidet sich, ob er im Datensatz landet (piper-record:
        # dataset.save_episode()) oder weggeworfen wird. SPACE zaehlt wie S.
        elif self.phase == "DECISION":
            if f.stop_requested:
                self.discarded += 1
                self._end("Stopp -> Versuch verworfen, Session beendet")
            elif f.save_requested or f.episode_end:
                f.save_requested = False
                f.episode_end = False
                if self.frames == 0:
                    self.log("0 Frames: nichts zu speichern, Versuch verworfen")
                else:
                    self.saved.append(self.frames)
                    self.log(f"Episode {len(self.saved) - 1} GESPEICHERT ({self.frames} Frames)")
                self.phase = "READY"
            elif f.discard_requested:
                f.discard_requested = False
                self.discarded += 1
                self.log("Versuch VERWORFEN")
                self.phase = "READY"

        # Ersatz fuer "Kamerabilder + Gelenkwerte aufnehmen": ein Frame pro Takt
        if self.phase == "RECORDING":
            self.frames += 1

    def _end(self, msg):
        self.phase = "ENDE"
        self.log(msg)


# ---------------------------------------------------------------------------
# Eingabequellen
# ---------------------------------------------------------------------------


def open_pedal(events: queue.Queue):
    """Erstes angeschlossenes Pedal oeffnen; (deck, Info-Text). deck ist None ohne Pedal.

    Die Bibliothek sucht per USB-ID (Elgato 0x0fd9, Pedal 0x0086). open()
    startet ihren eigenen Lese-Thread, der das Pedal abfragt und bei jeder
    AENDERUNG (treten UND loslassen) den Callback ruft."""
    try:
        from StreamDeck.DeviceManager import DeviceManager

        pedals = [d for d in DeviceManager().enumerate() if d.deck_type() == "Stream Deck Pedal"]
    except Exception as exc:
        return None, f"keine HID-Bibliothek ({exc})"
    if not pedals:
        return None, "kein Pedal gefunden - 1/2/3 simulieren die Pedale"
    deck = pedals[0]
    deck.open()
    # Der Callback laeuft im Lese-Thread der Bibliothek, nicht in unserer Schleife.
    # Darum hier nichts verarbeiten, nur in eine thread-sichere Queue legen;
    # die Hauptschleife holt die Ereignisse im naechsten Takt ab.
    deck.set_key_callback(lambda _deck, key, pressed: events.put(("pedal", key, pressed)))
    return deck, f"Pedal {deck.get_serial_number()} (FW {deck.get_firmware_version()})"


def open_uinput():
    """Virtuelle Tastatur ueber /dev/uinput anlegen (nur --uinput).

    Fuer Linux sieht das wie eine zweite angesteckte USB-Tastatur aus; was sie
    "tippt", landet im Fenster mit Fokus - wie bei einer echten Tastatur."""
    from evdev import UInput
    from evdev import ecodes as e

    codes = {"space": e.KEY_SPACE, "s": e.KEY_S, "d": e.KEY_D}
    ui = UInput({e.EV_KEY: list(codes.values())}, name="streamdeck-pedal-playground")

    def emit(key, pressed):
        ui.write(e.EV_KEY, codes[key], 1 if pressed else 0)  # 1 = runter, 0 = hoch
        ui.syn()  # "Ereignis komplett" - erst jetzt reicht der Kernel es weiter

    return ui, emit


def read_terminal_keys(fd):
    """Alle anliegenden Terminal-Tasten (cbreak, nicht blockierend).

    select() mit Timeout 0 fragt nur "liegt was an?" und wartet nie - die
    Schleife darf nicht haengen, wenn niemand tippt."""
    keys = []
    while select.select([fd], [], [], 0)[0]:
        chunk = os.read(fd, 64)
        if not chunk:
            break
        if chunk.startswith(b"\x1b") and len(chunk) > 1:
            continue  # Pfeiltasten & Co. ignorieren
        for b in chunk:
            ch = bytes([b])
            if ch == b" ":
                keys.append("space")
            elif ch in (b"\n", b"\r"):
                keys.append("enter")
            elif ch == b"\x1b":
                keys.append("esc")
            else:
                keys.append(ch.decode(errors="ignore").lower())
    return keys


# ---------------------------------------------------------------------------
# Anzeige (reine ANSI-Escape-Codes, keine Zusatzpakete)
# ---------------------------------------------------------------------------


def c(code, text):
    """Text einfaerben: 1 = fett, 31-36 = Farben, 90 = grau, 42;30 = schwarz auf gruen."""
    return f"\x1b[{code}m{text}\x1b[0m"


def render(session, flags, pedal_down, source, mode, log_lines):
    """Ganzen Bildschirm neu zeichnen (jeden Takt; \\x1b[H\\x1b[J = Cursor oben links, Rest leeren)."""
    phase = session.phase
    effects = KEY_EFFECT.get(phase, {})
    w = 20
    top, name, body, key, eff, bot = [], [], [], [], [], []
    for i in range(3):
        down = pedal_down[i]
        fill = c("42;30", " " * w) if down else " " * w
        top.append("┌" + "─" * w + "┐")
        name.append("│" + PEDAL_NAMES[i].center(w) + "│")
        body.append("│" + fill + "│")
        key.append("│" + c("1", f"-> {PEDAL_KEYS[i].upper()}".center(w)) + "│")
        eff.append("│" + effects.get(PEDAL_KEYS[i], "").center(w)[:w] + "│")
        bot.append("└" + "─" * w + "┘")

    out = ["\x1b[H\x1b[J"]
    out.append(c("1", "Stream-Deck-Pedal Spielwiese") + "   " + c("90", f"{source} | Modus: {mode}"))
    out.append("")
    for row in (top, name, body, body, key, eff, bot):
        out.append(" ".join(row))
    out.append("")

    status = c("1;" + PHASE_COLOR[phase], f"● {phase}")
    if phase == "RECORDING":
        t = time.monotonic() - session.attempt_start
        status += f"   {t:5.1f} s   {session.frames} Frames"
    elif phase == "DECISION":
        status += f"   {session.frames} Frames warten auf S = speichern / D = verwerfen"
    out.append("Phase:   " + status)

    def flag(n, v):
        return c("1;32", f"● {n}") if v else c("90", f"○ {n}")

    out.append(
        "Flags:   "
        + "  ".join(
            flag(n, v)
            for n, v in (
                ("episode_end", flags.episode_end),
                ("save", flags.save_requested),
                ("discard", flags.discard_requested),
                ("stop", flags.stop_requested),
            )
        )
    )
    out.append(f"Episoden: {len(session.saved)} gespeichert {session.saved}   {session.discarded} verworfen")
    out.append("")
    out.append(c("1", "Log"))
    out.extend(log_lines)
    out.append("")
    out.append(
        c("90", "Leertaste/Enter, s, d, q wie piper-record · 1/2/3 simulieren Pedale · Ctrl+C beendet")
    )
    sys.stdout.write("\r\n".join(out))
    sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--uinput", action="store_true", help="Pedal als virtuelle Linux-Tastatur")
    args = parser.parse_args()

    if not sys.stdin.isatty():
        sys.exit("Bitte in einem echten Terminal starten.")

    # -- Aufbau ----------------------------------------------------------------
    events = queue.Queue()  # Briefkasten zwischen Pedal-Thread und Hauptschleife
    log_lines = deque(maxlen=12)  # nur die letzten 12 Log-Zeilen anzeigen

    def log(msg):
        log_lines.append(f"{time.strftime('%H:%M:%S')}  {msg}")

    emit, ui = None, None
    if args.uinput:
        try:
            ui, emit = open_uinput()
        except Exception as exc:
            sys.exit(f"--uinput nicht moeglich: {exc}")
    deck, source = open_pedal(events)

    flags = RecordFlags()
    session = RecordSession(log)
    pedal_down = [False, False, False]
    sim_release = {}  # Pedal-Index -> Zeitpunkt der simulierten Freigabe
    mode = "uinput (Pedal tippt echte Tasten)" if emit else "direkt (Pedal -> Flags)"
    log("READY: Arme einrichten, Leertaste / mittleres Pedal startet einen Versuch")

    def on_pedal(i, pressed, simulated=False):
        """Ein Pedal-Ereignis verarbeiten - hier unterscheiden sich die beiden Modi:
        direkt:  Pedal -> PEDAL_KEYS -> flags.feed()  (so wuerde ein Pedal-Backend
                 in SessionKeys arbeiten)
        uinput:  Pedal -> virtuelle Tastatur -> Linux -> Fenster mit Fokus. Die
                 Flags setzt dann erst read_terminal_keys(), WENN das Terminal den
                 Fokus hat - genau der Weg, den piper-record heute nutzt."""
        if i not in PEDAL_KEYS:
            return
        pedal_down[i] = pressed
        k = PEDAL_KEYS[i]
        tag = " (simuliert)" if simulated else ""
        if emit:
            emit(k, pressed)  # runter UND hoch weitergeben, wie eine echte Taste
            if pressed:
                log(f"Pedal {PEDAL_NAMES[i]}{tag} ↓ -> uinput tippt {k.upper()}")
        elif pressed:  # nur das Treten zaehlt, Loslassen ist nur fuer die Anzeige
            log(f"Pedal {PEDAL_NAMES[i]}{tag} ↓ -> {k.upper()}")
            flags.feed(k)

    # Terminal in cbreak-Modus: jede Taste kommt sofort an (ohne Enter) und wird
    # nicht angezeigt. Den alten Zustand merken - er MUSS am Ende zurueck, sonst
    # bleibt das Terminal "kaputt" (kein Echo).
    fd = sys.stdin.fileno()
    old_term = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    sys.stdout.write("\x1b[?1049h\x1b[?25l")  # Alternativer Bildschirm, Cursor aus

    # -- Hauptschleife: ein Durchlauf = ein Takt (1/30 s), wie in piper-record --
    try:
        while True:
            tick_start = time.monotonic()

            # 1) Pedal-Ereignisse aus der Queue abholen
            while not events.empty():
                _, i, pressed = events.get()
                on_pedal(i, pressed)
            # 2) simulierte Pedale (1/2/3) nach SIM_PRESS_S wieder "loslassen"
            for i, t in list(sim_release.items()):
                if tick_start >= t:
                    del sim_release[i]
                    on_pedal(i, False, simulated=True)

            # 3) Terminal-Tasten: 1/2/3 tun so, als waere ein Pedal getreten,
            #    der Rest geht direkt an die Flags (wie der stdin-Weg in keys.py)
            for k in read_terminal_keys(fd):
                if k in ("1", "2", "3"):
                    i = int(k) - 1
                    on_pedal(i, True, simulated=True)
                    sim_release[i] = tick_start + SIM_PRESS_S
                elif k in ("space", "enter", "s", "d", "q", "esc"):
                    log(f"Terminal-Taste {k.upper()}")
                    flags.feed(k)

            # 4) Zustandsmaschine: Flags auswerten, Phase wechseln, Frame zaehlen
            if session.phase != "ENDE":
                session.tick(flags)
            # 5) Bildschirm zeichnen
            render(session, flags, pedal_down, source, mode, log_lines)
            if session.phase == "ENDE":
                time.sleep(1.5)  # Endzustand kurz stehen lassen
                break

            # 6) Rest des Taktes schlafen, damit es genau 30 Durchlaeufe/s sind
            time.sleep(max(0.0, 1.0 / FPS - (time.monotonic() - tick_start)))
    except KeyboardInterrupt:
        pass
    finally:
        # Aufraeumen passiert IMMER (auch bei Ctrl+C oder Absturz)
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        termios.tcsetattr(fd, termios.TCSADRAIN, old_term)
        if deck is not None:
            deck.close()
        if ui is not None:
            ui.close()

    print(f"Session vorbei: {len(session.saved)} gespeichert {session.saved}, {session.discarded} verworfen.")


if __name__ == "__main__":
    main()
