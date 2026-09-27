"""Entsperren einer LUKS-VM über die serielle Konsole.

Die Konsolenausgabe wird nur im Speicher gepuffert und nie geloggt:
cryptsetup echot die Passphrase als Sternchen, das verrät ihre Länge.
"""

import codecs
import re
import time

from .backends.base import BackendError, ConsoleBusy, ConsoleClosed

# Puffergrenze; genug für jede Boot-Phase, verhindert unbegrenztes Wachstum.
MAX_BUFFER = 64 * 1024

# Zustände (auch Job-Zustände des Start-Jobs)
STARTING = "starting"
WAITING_PROMPT = "waiting_prompt"
UNLOCKING = "unlocking"
UNLOCKED = "unlocked"
WRONG_KEY = "wrong_key"


class UnlockFailed(Exception):
    """Entsperren gescheitert; die Meldung ist für Nutzer gedacht."""


class WrongKey(UnlockFailed):
    """Passphrase falsch und keine Wiederholung erlaubt."""


class ConsoleWatcher:
    """Puffert Konsolenausgabe und sucht darin nach Mustern (nicht zeilenweise)."""

    def __init__(self, serial, cancel_event=None):
        self._serial = serial
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buf = ""
        self._cancel = cancel_event

    def wait_for(self, patterns, timeout):
        """Wartet, bis eines der Muster im Puffer steht.

        patterns: dict name -> kompiliertes Regex. Liefert (name, match) des
        frühesten Treffers und verwirft den Puffer bis zu dessen Ende, oder
        (None, None) bei Timeout. Wirft ConsoleClosed, wenn die Konsole zugeht.
        """
        deadline = time.monotonic() + timeout
        while True:
            hit = self._search(patterns)
            if hit:
                return hit
            if self._cancel is not None and self._cancel.is_set():
                raise UnlockFailed("Vorgang abgebrochen.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, None
            chunk = self._serial.read(min(remaining, 0.5))
            if chunk:
                self._buf += self._decoder.decode(chunk)
                if len(self._buf) > MAX_BUFFER:
                    self._buf = self._buf[-MAX_BUFFER:]

    def _search(self, patterns):
        best = None
        for name, rx in patterns.items():
            m = rx.search(self._buf)
            if m and (best is None or m.start() < best[1].start()):
                best = (name, m)
        if best:
            self._buf = self._buf[best[1].end():]
        return best

    def write(self, data):
        self._serial.write(data)

    def discard(self):
        self._buf = ""


def compile_patterns(cfg):
    return {
        "prompt": re.compile(cfg.prompt_regex),
        "success": re.compile(cfg.success_regex),
        "fail": re.compile(cfg.fail_regex),
        "exceeded": re.compile(cfg.tries_exceeded_regex),
    }


def _type_passphrase(watcher, passphrase):
    data = bytearray(passphrase.encode("utf-8"))
    data += b"\n"
    try:
        watcher.write(bytes(data))
    finally:
        for i in range(len(data)):
            data[i] = 0


def unlock(serial, cfg, passphrase, on_state, ask_again=None, cancel_event=None):
    """Wartet auf den LUKS-Prompt und gibt die Passphrase ein.

    on_state(state) meldet Zustandswechsel. Nach einer falschen Passphrase
    wird ask_again() aufgerufen (blockiert bis zur neuen Eingabe, None =
    Abbruch). Ohne ask_again wird WrongKey geworfen.

    Liefert die Passphrase, die funktioniert hat (der Klon braucht sie
    danach noch); Aufrufer ohne Bedarf verwerfen sie sofort.
    """
    p = compile_patterns(cfg)
    watcher = ConsoleWatcher(serial, cancel_event)
    try:
        on_state(WAITING_PROMPT)
        name, _ = watcher.wait_for({"prompt": p["prompt"]}, cfg.prompt_timeout)
        if name is None:
            raise UnlockFailed(
                f"Kein LUKS-Prompt innerhalb von {cfg.prompt_timeout:g} s erkannt. "
                "Ist die serielle Konsole im Gast aktiv (console=ttyS0 zuletzt)?"
            )
        while True:
            on_state(UNLOCKING)
            _type_passphrase(watcher, passphrase)
            name, _ = watcher.wait_for(
                {"success": p["success"], "fail": p["fail"]}, cfg.unlock_timeout
            )
            if name == "success":
                on_state(UNLOCKED)
                return passphrase
            passphrase = None
            if name is None:
                raise UnlockFailed(
                    f"Nach der Eingabe kam innerhalb von {cfg.unlock_timeout:g} s "
                    "weder Erfolg noch Fehler zurück."
                )
            # Falsche Passphrase: Kommt ein neuer Prompt oder ist das Limit erreicht?
            name, _ = watcher.wait_for(
                {"prompt": p["prompt"], "exceeded": p["exceeded"]}, cfg.unlock_timeout
            )
            if name != "prompt":
                raise UnlockFailed(
                    "Passphrase falsch und keine weiteren Versuche möglich "
                    "(Limit im Initramfs erreicht). Die VM hängt im Initramfs: "
                    "hart ausschalten und neu starten."
                )
            if ask_again is None:
                raise WrongKey("Passphrase falsch.")
            on_state(WRONG_KEY)
            passphrase = ask_again()
            if passphrase is None:
                raise UnlockFailed(
                    "Keine neue Passphrase eingegeben. Die VM wartet weiter am "
                    "LUKS-Prompt; hart ausschalten und neu starten."
                )
    except ConsoleClosed:
        raise UnlockFailed("Die serielle Konsole wurde geschlossen (VM ausgeschaltet?).")
    finally:
        watcher.discard()


CONSOLE_BUSY_MSG = (
    "Die serielle Konsole der VM ist belegt (z. B. offenes virsh console). "
    "Bitte dort trennen; vmdash übernimmt sie nicht gewaltsam."
)


def boot_and_unlock(backend, cfg, name, passphrase, on_state, ask_again=None, cancel_event=None):
    """Startet eine ausgeschaltete VM und entsperrt sie.

    Die VM wird angehalten gestartet, dann wird die Konsole geöffnet und erst
    danach fortgesetzt; so geht keine Ausgabe vor dem Prompt verloren.
    """
    on_state(STARTING)
    backend.start(name, paused=True)
    try:
        serial = backend.open_serial(name)
    except ConsoleBusy:
        _destroy_quietly(backend, name)
        raise UnlockFailed(CONSOLE_BUSY_MSG)
    except BackendError as e:
        _destroy_quietly(backend, name)
        raise UnlockFailed(f"Konsole konnte nicht geöffnet werden: {e}")
    try:
        backend.resume(name)
        return unlock(serial, cfg, passphrase, on_state, ask_again, cancel_event)
    finally:
        serial.close()


def reboot_and_unlock(backend, cfg, name, passphrase, on_state, cancel_event=None):
    """Startet eine laufende VM über den Guest-Agent neu und entsperrt sie.

    Keine Wiederholung bei falscher Passphrase (WrongKey).
    """
    try:
        serial = backend.open_serial(name)
    except ConsoleBusy:
        raise UnlockFailed(CONSOLE_BUSY_MSG)
    try:
        backend.reboot(name)
        return unlock(serial, cfg, passphrase, on_state, None, cancel_event)
    finally:
        serial.close()


def _destroy_quietly(backend, name):
    # Die VM ist noch angehalten und hat nichts ausgeführt; ausschalten ist gefahrlos.
    try:
        backend.destroy(name)
    except BackendError:
        pass
