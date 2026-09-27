"""Entsperr-Ablauf gegen einen Fake-Serial-Stream aus dem echten Mitschnitt."""

import re
import threading

import pytest

from app.backends.base import ConsoleClosed, SerialConsole
from app.config import DEFAULT_BOOT_CAPTURE
from app.unlock import UnlockFailed, WrongKey, unlock
from tests.conftest import make_config

CAPTURE = DEFAULT_BOOT_CAPTURE.read_text(encoding="utf-8")


def capture_parts():
    """Zerlegt den Mitschnitt: Ausgabe vor dem Prompt, Prompt, Fehler-, Erfolgstext."""
    lines = CAPTURE.splitlines(keepends=True)
    prompts = [i for i, l in enumerate(lines) if l.startswith("Please unlock disk")]
    first = prompts[0]
    before = "".join(lines[:first])
    prompt = re.match(r"Please unlock disk \S+:", lines[first]).group(0)
    stars_1 = lines[first][len(prompt):]
    fail = "".join(lines[first + 1:prompts[1]])  # Fehlermeldung + Leerzeile
    ok_idx = next(i for i, l in enumerate(lines) if "set up successfully" in l)
    success = "".join(lines[ok_idx:ok_idx + 5])
    return before, prompt, stars_1, fail, success


class FakeSerial(SerialConsole):
    """Spielt den Mitschnitt ab und reagiert auf Eingaben wie cryptroot."""

    def __init__(self, chunks, wrong=("falsch",), max_tries=3, respond=True):
        self.out = bytearray("".join(chunks).encode())
        self.written = []
        self.tries = 0
        self.wrong = set(wrong)
        self.max_tries = max_tries
        self.respond = respond
        self.closed_by_vm = False
        self.lock = threading.Lock()
        self.chunk_size = 7  # kleine Stücke: der Prompt kommt zerteilt an

    def read(self, timeout):
        with self.lock:
            if self.out:
                data = bytes(self.out[: self.chunk_size])
                del self.out[: self.chunk_size]
                return data
            if self.closed_by_vm:
                raise ConsoleClosed("eof")
        return b""

    def write(self, data):
        self.written.append(data)
        if not self.respond:
            return
        _before, prompt, _stars, fail, success = capture_parts()
        p = data.decode().rstrip("\n")
        with self.lock:
            self.out += ("*" * len(p) + "\n").encode()
            if p in self.wrong:
                self.tries += 1
                self.out += fail.encode()
                if self.tries >= self.max_tries:
                    self.out += b"cryptsetup: ERROR: vda3_crypt: maximum number of tries exceeded\n"
                else:
                    self.out += prompt.encode()
            else:
                self.out += success.encode()

    def close(self):
        pass


@pytest.fixture
def cfg():
    return make_config(prompt_timeout=1, unlock_timeout=1)


def boot_chunks():
    before, prompt, *_ = capture_parts()
    return [before, prompt]


def test_default_regexes_match_capture(cfg):
    assert len(re.findall(cfg.prompt_regex, CAPTURE)) == 2
    assert re.search(cfg.prompt_regex, CAPTURE).group(1) == "vda3_crypt"
    assert len(re.findall(cfg.fail_regex, CAPTURE)) == 1
    assert re.search(cfg.success_regex, CAPTURE).group(1) == "vda3_crypt"


def test_prompt_without_newline_is_detected(cfg):
    before, prompt, *_ = capture_parts()
    assert not prompt.endswith("\n")
    serial = FakeSerial([before, prompt])
    states = []
    result = unlock(serial, cfg, "richtig", states.append)
    assert result == "richtig"
    assert states == ["waiting_prompt", "unlocking", "unlocked"]
    assert serial.written == [b"richtig\n"]


def test_wrong_then_right_with_retry(cfg):
    serial = FakeSerial(boot_chunks())
    states = []
    answers = iter(["richtig"])
    result = unlock(serial, cfg, "falsch", states.append, ask_again=lambda: next(answers))
    assert result == "richtig"
    assert states == ["waiting_prompt", "unlocking", "wrong_key", "unlocking", "unlocked"]
    assert serial.written == [b"falsch\n", b"richtig\n"]


def test_wrong_without_retry_raises_wrong_key(cfg):
    with pytest.raises(WrongKey):
        unlock(FakeSerial(boot_chunks()), cfg, "falsch", lambda s: None)


def test_retry_cancelled(cfg):
    with pytest.raises(UnlockFailed, match="Keine neue Passphrase"):
        unlock(FakeSerial(boot_chunks()), cfg, "falsch", lambda s: None, ask_again=lambda: None)


def test_tries_exceeded(cfg):
    serial = FakeSerial(boot_chunks(), max_tries=3)
    with pytest.raises(UnlockFailed, match="Limit im Initramfs"):
        unlock(serial, cfg, "falsch", lambda s: None, ask_again=lambda: "falsch")
    assert len(serial.written) == 3


def test_no_prompt_timeout(cfg):
    before, *_ = capture_parts()
    with pytest.raises(UnlockFailed, match="Kein LUKS-Prompt"):
        unlock(FakeSerial([before]), cfg, "x", lambda s: None)


def test_no_answer_after_input(cfg):
    serial = FakeSerial(boot_chunks(), respond=False)
    with pytest.raises(UnlockFailed, match="weder Erfolg noch Fehler"):
        unlock(serial, cfg, "x", lambda s: None)


def test_console_closed(cfg):
    serial = FakeSerial(["BdsDxe: loading\n"])
    serial.closed_by_vm = True
    with pytest.raises(UnlockFailed, match="Konsole wurde geschlossen"):
        unlock(serial, cfg, "x", lambda s: None)


def test_cancel_event(cfg):
    ev = threading.Event()
    ev.set()
    with pytest.raises(UnlockFailed, match="abgebrochen"):
        unlock(FakeSerial([]), cfg, "x", lambda s: None, cancel_event=ev)
