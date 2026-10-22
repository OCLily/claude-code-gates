#!/usr/bin/env python3
# Shared decision logger for the gates: one JSON line per decision under
# ~/.claude/metrics/gates/ (90 days, on by default; CLAUDE_GATE_LOG_DISABLE=1 turns it off).
# deny/ask: secret-like values masked. allow: raw command text, cut at 1000 characters.
"""Gemeinsamer Entscheidungs-Logger fuer die Gate-Schicht.

Ein Gate ruft log_decision() unmittelbar vor seiner Ausgabe auf. Eine Zeile
JSON pro Entscheidung, tagesweise unter ~/.claude/metrics/gates/.

ABSOLUT fail-open: jeder Fehler hier wird verschluckt. Diese Gates haengen an
PreToolUse(Bash) — ein Logger, der ein Gate zum Absturz bringt, sperrt im
schlimmsten Fall jeden Bash-Aufruf aus. Deshalb: kein Reraise, kein schmales
except.
Das gilt auch fuer Maskierung und Musterimport: wirft die Maskierung, wird der
Eintrag OHNE Zielfeld geschrieben (mask_error) — nie mit unmaskiertem Klartext,
nie als Exception. Laesst sich secret_patterns nicht laden, gilt auch fuer
Treffer die Grenze 1000 (mask=unavailable): ohne Maskierung keine zusaetzliche
Flaeche.

Laufzeit: ein stat + ein append pro Aufruf. Das Aufraeumen (Dateien aelter als
RETENTION_DAYS) laeuft nur, wenn die Tagesdatei neu angelegt wurde — also
einmal pro Tag, nicht bei jedem Tool-Call.

Das Protokoll ist standardmaessig AN. Zwei Env-Schalter, beide nur fuer
Tests/Notfall: CLAUDE_GATE_LOG_DISABLE=1 schaltet das Protokoll ganz ab,
CLAUDE_GATE_LOG_DIR lenkt es um. Beides aendert keine Entscheidung, und wer Env
setzen kann, konnte das Gate ohnehin schon umgehen — also keine neue
Angriffsflaeche.

Was im Protokoll steht: bei deny/ask das Kommando mit maskierten
secret-aehnlichen Werten, bei allow der Kommandotext UNMASKIERT (bei 1000
Zeichen gekappt). Ohne das Kommando ist "echter Treffer oder Fehlalarm" nicht
beantwortbar. Wer Klartext-Kommandos nicht auf der Platte haben will, setzt
CLAUDE_GATE_LOG_DISABLE=1.

Herkunft (origin): "hook", wenn das stdin-JSON des Hook-Runners einen
session_id-SCHLUESSEL trug, sonst "direct" (Selftest, Skriptaufruf, Sonde).
Das Feld "session" ist bei Direktaufruf leer. Ein eigenes Feld macht
Sondenaufrufe direkt erkennbar; eine Auswertung muss die Herkunft nicht aus der
LEERE eines anderen Feldes erschliessen.

Zwei Grenzen, nach Entscheidung getrennt:
  * deny/ask (Treffer, ein kleiner Bruchteil der Aufrufe) -> NICHT gekappt,
    maskiert. Es greift nur die Pathologie-Schranke PATHOLOGICAL_MAX = 65536.
  * allow (Nenner)                         -> LEGACY_MAX_TARGET = 1000.
Begruendung: ein Urteil ueber einen Treffer ("echter Treffer oder Fehlalarm")
darf nie an einem abgeschnittenen String haengen. Fuer allow-Zeilen braucht eine
Auswertung nur den Nenner, nicht den Volltext; so waechst die Secret-Flaeche
nicht auf alle Allow-Zeilen.

Fuer Treffer gibt es deshalb keine Kappung:
  * Treffer sind ein winziger Anteil der Aufrufe und tragen nur einen kleinen
    Bruchteil des Protokollvolumens.
  * Jede Grenze > 0 laesst den Fall offen, dass ein Urteil an einem
    abgeschnittenen String haengt; nur keine Grenze schliesst ihn.
  * Das Maskierungs-Argument gilt sogar staerker: ungekappte Treffer bedeuten
    mehr Text, der maskiert werden muss. Die Maskierung laeuft im Treffer-Zweig
    auf dem UNGEKAPPTEN Rohtext (gekappt wird erst bei der Feldzuweisung);
    laenger werden nur json.dumps und der write.

PATHOLOGICAL_MAX = 65536 ist KEINE Perzentil-Grenze, sondern eine
Pathologie-Schranke mit zwei Zwecken:
  (a) Sie liegt ueber jedem gewoehnlichen Kommando und greift nur bei
      pathologisch langen Eingaben. Ein Selftest haelt
      PATHOLOGICAL_MAX >= LONG_COMMAND_LEN (40000 Zeichen) fest, damit diese
      Eigenschaft falsifizierbar bleibt statt Behauptung zu sein.
  (b) Laufzeit. Die Maskierung ist linear, ohne Backtracking-Klippe.
      Ohne jede Schranke kostet ein N-Zeichen-Kommando also N x (kleine
      Konstante) im deny-Pfad und N Bytes in EINER Logzeile, mit N durch nichts
      begrenzt. Die Schranke bindet diese zwei sonst unbegrenzten Groessen — das
      ist ihr ganzer Zweck, nicht Volumensparen.
Ueber 64 KB wird gekappt, mit target_truncated/target_len — in BEIDEN Pfaden,
auch im Allow-Pfad bei 1000 —, damit eine Auswertung NIEMALS unbemerkt ueber
einen abgeschnittenen String urteilt.

Die Maskierung laeuft nur im Treffer-Zweig, und das Muster-Modul wird lazy erst
dort importiert: der Regex-Compile kostet pro Gate-Prozess Laufzeit, die sonst
bei JEDEM Bash-Tool-Call und jedem der Bash-Gates anfiele — bezahlt auf 100 % der
Aufrufe fuer einen Nutzen, der nur bei Treffern anfaellt. Der Allow-Pfad bleibt
davon unberuehrt.

Maskierung (nur im Treffer-Zweig): die Muster kommen vollstaendig aus
secret_patterns.py (wortgleich aus commit-secret-sweep.py uebernommen) — kein
zweiter, selbstverfasster Mustersatz, der nur die Faelle kennt, die sich der
Autor vorstellen konnte.
Was sie NICHT faengt, steht im Docstring von secret_patterns.py: Passwoerter in
Prosa, Secrets mit Sonderzeichen im Rumpf (JWT, base64 mit +/=), mehrzeilige
PEM-Bloecke (es gibt KEIN -----BEGIN-Muster in HIGH_CONF), unbekannte
Vendor-Formate, Tokens unter 24 Zeichen ohne Prefix, und alles Indirekte (Hosts,
Pfade, Mailadressen). Maskierung nach Mustern ist prinzipiell unvollstaendig;
sie senkt die durch ungekappte Treffer-Eintraege entstehende Flaeche, sie
beseitigt sie nicht.
"""

import json
import os
import sys
import time

DISABLE_ENV = "CLAUDE_GATE_LOG_DISABLE"
DIR_ENV = "CLAUDE_GATE_LOG_DIR"
LOG_DIR = (os.environ.get(DIR_ENV)
           or os.path.join(os.path.expanduser("~"), ".claude", "metrics", "gates"))
RETENTION_DAYS = 90
PATHOLOGICAL_MAX = 65536
LEGACY_MAX_TARGET = 1000
LONG_COMMAND_LEN = 40000
HIT_DECISIONS = ("deny", "ask")

# [] = noch nicht versucht, [None] = nicht ladbar, [modul] = geladen.
_PATTERNS = []


def patterns():
    """secret_patterns oder None. Lazy: der Regex-Compile faellt nur im
    Treffer-Zweig an, nicht auf dem Allow-Pfad."""
    if not _PATTERNS:
        try:
            lib = os.path.dirname(os.path.abspath(__file__))
            if lib not in sys.path:
                sys.path.append(lib)
            import secret_patterns
            _PATTERNS.append(secret_patterns)
        except Exception:
            _PATTERNS.append(None)
    return _PATTERNS[0]


def _is_day_file(name):
    return (len(name) == 16 and name.endswith(".jsonl")
            and name[4] == "-" and name[7] == "-"
            and name[0:4].isdigit() and name[5:7].isdigit()
            and name[8:10].isdigit())


def _prune(now, log_dir):
    cutoff = now - RETENTION_DAYS * 86400
    for name in os.listdir(log_dir):
        if not _is_day_file(name):
            continue
        victim = os.path.join(log_dir, name)
        try:
            if os.path.getmtime(victim) < cutoff:
                os.remove(victim)
        except OSError:
            pass


def _target_of(tool_input):
    for key in ("command", "file_path", "path", "notebook_path"):
        value = tool_input.get(key)
        if value:
            return value
    return ""


def _redact(token):
    return token[:4] + "[secret:%d]" % len(token)


def mask_secrets(text, pat):
    """(maskierter Text, Zahl der Ersetzungen). Reihenfolge wie im Sweep:
    HIGH_CONF zuerst, GENERIC nur fuer das, was die Entropie-Huerde nimmt."""
    hits = 0
    for rx, _label in pat.HIGH_CONF:
        text, n = rx.subn(lambda m: _redact(m.group(0)), text)
        hits += n
    count = [0]

    def generic(match):
        token = match.group(0)
        if pat.looks_generic_secret(token):
            count[0] += 1
            return _redact(token)
        return token

    text = pat.GENERIC.sub(generic, text)
    return text, hits + count[0]


def log_decision(gate, decision, reason, data=None, *, target=None, tool=None,
                 log_dir=None):
    try:
        if os.environ.get(DISABLE_ENV) == "1":
            return
        log_dir = log_dir or LOG_DIR
        data = data if isinstance(data, dict) else {}
        tool_input = data.get("tool_input")
        tool_input = tool_input if isinstance(tool_input, dict) else {}
        if target is None:
            target = _target_of(tool_input)
        raw = str(target)
        shown, hits, limit, unavailable, mask_error = raw, 0, LEGACY_MAX_TARGET, False, False
        if decision in HIT_DECISIONS:
            pat = patterns()
            if pat is None:
                unavailable = True
            else:
                limit = PATHOLOGICAL_MAX
                try:
                    shown, hits = mask_secrets(raw, pat)
                except Exception:
                    shown, mask_error = "", True
        now = time.time()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now))
        record = {
            "ts": stamp,
            "gate": gate,
            "decision": decision,
            "reason": reason,
            "mode": data.get("permission_mode") or "",
            "session": data.get("session_id") or "",
            "origin": "hook" if "session_id" in data else "direct",
            "cwd": data.get("cwd") or "",
            "tool": tool or data.get("tool_name") or "",
            "target": shown[:limit],
        }
        if len(shown) > limit:
            record["target_truncated"] = True
            record["target_len"] = len(raw)
        if hits:
            record["target_masked"] = hits
        if mask_error:
            record["mask_error"] = True
        if unavailable:
            record["mask"] = "unavailable"
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        path = os.path.join(log_dir, stamp[0:10] + ".jsonl")
        fresh = not os.path.exists(path)
        if fresh:
            os.makedirs(log_dir, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
        if fresh:
            _prune(now, log_dir)
    except Exception:
        pass


def _selftest():
    import glob
    import re
    import subprocess
    import tempfile

    os.environ.pop(DISABLE_ENV, None)
    failures = []

    def check(name, ok):
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            failures.append(name)

    def one(tmp, *args, **kwargs):
        log_decision(*args, log_dir=tmp, **kwargs)
        files = glob.glob(os.path.join(tmp, "*.jsonl"))
        return json.loads(open(files[0], encoding="utf-8").read().splitlines()[-1])

    print("== Grundfall: eine Entscheidung erzeugt genau eine Zeile ==")
    with tempfile.TemporaryDirectory() as tmp:
        payload = {"tool_input": {"command": "rm -rf /Users/alice/vault"},
                   "permission_mode": "bypassPermissions",
                   "session_id": "sess-1", "cwd": "/Users/alice",
                   "tool_name": "Bash"}
        log_decision("destructive-command-gate", "deny", "rm-rf", payload,
                     log_dir=tmp)
        files = glob.glob(os.path.join(tmp, "*.jsonl"))
        check("genau eine Tagesdatei angelegt", len(files) == 1)
        lines = open(files[0], encoding="utf-8").read().splitlines()
        check("genau eine Zeile", len(lines) == 1)
        rec = json.loads(lines[0])
        check("gate/decision/reason korrekt",
              rec["gate"] == "destructive-command-gate"
              and rec["decision"] == "deny" and rec["reason"] == "rm-rf")
        check("Kommando im Klartext", rec["target"].startswith("rm -rf"))
        check("Modus/Session/cwd/Tool uebernommen",
              rec["mode"] == "bypassPermissions" and rec["session"] == "sess-1"
              and rec["cwd"] == "/Users/alice" and rec["tool"] == "Bash")
        check("Zeitstempel traegt den Tag der Datei",
              os.path.basename(files[0]) == rec["ts"][0:10] + ".jsonl")
        log_decision("push-gate", "allow", "no-hit", payload, log_dir=tmp)
        lines = open(files[0], encoding="utf-8").read().splitlines()
        check("allow wird ebenfalls protokolliert (Nenner der Trefferquote)",
              len(lines) == 2 and json.loads(lines[1])["decision"] == "allow")

    print("\n== Write|Edit-Form: file_path statt command ==")
    with tempfile.TemporaryDirectory() as tmp:
        log_decision("example-gate", "deny", "guarded-path",
                     {"tool_input": {"file_path": "/Users/alice/.claude/CLAUDE.md"}},
                     log_dir=tmp)
        rec = json.loads(open(glob.glob(os.path.join(tmp, "*.jsonl"))[0],
                              encoding="utf-8").readline())
        check("file_path landet im target-Feld",
              rec["target"].endswith("CLAUDE.md"))

    print("\n== Herkunft: Hook-Aufruf vs. Direktaufruf (Sonde) ==")
    with tempfile.TemporaryDirectory() as tmp:
        rec = one(tmp, "x", "deny", "y",
                  {"tool_input": {"command": "z"}, "session_id": "sess-9"})
        check("session_id im stdin-JSON -> origin=hook", rec["origin"] == "hook")
        check("session bleibt unveraendert gefuellt", rec["session"] == "sess-9")
        rec = one(tmp, "x", "deny", "y", {"tool_input": {"command": "z"}})
        check("kein session_id -> origin=direct", rec["origin"] == "direct")
        check("session bleibt bei Direktaufruf leer", rec["session"] == "")
        rec = one(tmp, "x", "deny", "y", None)
        check("data=None -> origin=direct", rec["origin"] == "direct")
        rec = one(tmp, "x", "deny", "y", {"tool_input": {"command": "z"},
                                          "session_id": ""})
        check("leeres session_id ist trotzdem ein Hook-Aufruf (Schluessel zaehlt, "
              "nicht der Wert)",
              rec["origin"] == "hook" and rec["session"] == "")

    print("\n== Treffer werden nicht gekappt ==")
    with tempfile.TemporaryDirectory() as tmp:
        mid = "echo " + "a" * 2000
        rec = one(tmp, "x", "deny", "y", {"tool_input": {"command": mid}})
        check("2005-Zeichen-deny wird vollstaendig protokolliert",
              rec["target"] == mid)
        check("und traegt keine Kappungs-Felder",
              "target_truncated" not in rec and "target_len" not in rec)
        for n in (4001, 6000, LONG_COMMAND_LEN):
            cmd = "echo " + "a" * (n - 5)
            rec = one(tmp, "x", "deny", "y", {"tool_input": {"command": cmd}})
            check("%d Zeichen bleiben vollstaendig (keine Kappung bei "
                  "Treffern)" % n,
                  rec["target"] == cmd and "target_truncated" not in rec)
        rec = one(tmp, "x", "ask", "y",
                  {"tool_input": {"command": "echo " + "a" * 9000}})
        check("ask zaehlt wie deny als Treffer (ebenfalls ungekappt)",
              len(rec["target"]) == 9005)

    print("\n== Pathologie-Schranke: greift erst bei pathologisch langen "
          "Kommandos ==")
    with tempfile.TemporaryDirectory() as tmp:
        check("die Schranke liegt ueber einem sehr langen Kommando (%d)"
              % LONG_COMMAND_LEN, PATHOLOGICAL_MAX >= LONG_COMMAND_LEN)
        huge = "echo " + "a" * (PATHOLOGICAL_MAX + 5000)
        rec = one(tmp, "x", "deny", "y", {"tool_input": {"command": huge}})
        check("jenseits von PATHOLOGICAL_MAX wird gekappt",
              len(rec["target"]) == PATHOLOGICAL_MAX)
        check("auch diese Kappung setzt dieselben Felder (nie still)",
              rec.get("target_truncated") is True
              and rec.get("target_len") == len(huge))
        exact = "a" * PATHOLOGICAL_MAX
        rec = one(tmp, "x", "deny", "y", {"tool_input": {"command": exact}})
        check("genau PATHOLOGICAL_MAX ist noch unversehrt und unmarkiert",
              len(rec["target"]) == PATHOLOGICAL_MAX
              and "target_truncated" not in rec)

    print("\n== Zwei Grenzen: Treffer vs. Nenner ==")
    with tempfile.TemporaryDirectory() as tmp:
        mid = "echo " + "a" * 2000
        rec = one(tmp, "x", "allow", "y", {"tool_input": {"command": mid}})
        check("2005-Zeichen-allow wird bei 1000 gekappt (Nenner braucht keinen "
              "Volltext)", len(rec["target"]) == LEGACY_MAX_TARGET)
        check("und traegt die Kappungs-Felder (nie still)",
              rec.get("target_truncated") is True
              and rec.get("target_len") == len(mid))
        long_cmd = "echo " + "a" * 5000
        rec = one(tmp, "x", "allow", "y", {"tool_input": {"command": long_cmd}})
        check("auch sehr langes allow bleibt bei der Grenze 1000",
              len(rec["target"]) == LEGACY_MAX_TARGET
              and rec.get("target_len") == len(long_cmd))
        rec = one(tmp, "x", "deny", "y", {"tool_input": {"command": long_cmd}})
        check("dasselbe Kommando als Treffer vollstaendig",
              rec["target"] == long_cmd)

    print("\n== Maskierung: beide Seiten, nur im Treffer-Zweig ==")
    with tempfile.TemporaryDirectory() as tmp:
        fake_aws = "AKIA" + "QWERTYUIOPASDFGH"
        fake_hex = "d41d8cd98f00b204e9800998ecf8427e" * 2
        cmd = "aws configure set key %s && curl -H 'x: %s' host" % (fake_aws,
                                                                   fake_hex)
        rec = one(tmp, "x", "deny", "y", {"tool_input": {"command": cmd}})
        check("AWS-Key-Form maskiert", fake_aws not in rec["target"])
        check("hex64 maskiert", fake_hex not in rec["target"])
        check("Maskierung ist gezaehlt", rec.get("target_masked", 0) >= 2)
        check("Geruest des Kommandos bleibt lesbar",
              rec["target"].startswith("aws configure set key AKIA[secret:")
              and rec["target"].endswith("host"))
        rec = one(tmp, "x", "ask", "y", {"tool_input": {"command": cmd}})
        check("ask maskiert ebenfalls", fake_aws not in rec["target"])
        rec = one(tmp, "x", "allow", "y", {"tool_input": {"command": cmd}})
        check("allow wird NICHT maskiert (keine neue Flaeche, keine Laufzeit)",
              fake_aws in rec["target"] and "target_masked" not in rec)
        plain = ("git -C /Users/alice/.claude log --oneline -20 -- "
                 "scripts/lib/gate_log.py && echo Done2026 fertig")
        rec = one(tmp, "x", "deny", "y", {"tool_input": {"command": plain}})
        check("unverdaechtiger Treffer bleibt Zeichen fuer Zeichen gleich",
              rec["target"] == plain)
        check("unverdaechtiger Treffer setzt kein Masken-Feld",
              "target_masked" not in rec)
        rec = one(tmp, "x", "deny", "y",
                  {"tool_input": {"file_path": "/Users/alice/.claude/CLAUDE.md"}})
        check("Pfade werden nicht maskiert",
              rec["target"] == "/Users/alice/.claude/CLAUDE.md")
        check("Masken-Muster kommen aus secret_patterns (SSOT geladen)",
              patterns() is not None and len(patterns().HIGH_CONF) >= 17)

    print("\n== Lazy Import: der Allow-Pfad laedt die Muster nicht ==")
    saved_cache = list(_PATTERNS)
    del _PATTERNS[:]
    with tempfile.TemporaryDirectory() as tmp:
        log_decision("x", "allow", "y", {"tool_input": {"command": "echo hi"}},
                     log_dir=tmp)
        check("allow laesst den Muster-Cache unberuehrt (kein Regex-Compile)",
              not _PATTERNS)
        log_decision("x", "deny", "y", {"tool_input": {"command": "echo hi"}},
                     log_dir=tmp)
        check("deny laedt die Muster genau einmal", len(_PATTERNS) == 1)
    del _PATTERNS[:]
    _PATTERNS.extend(saved_cache)

    print("\n== Maskierung darf das Gate nicht mitreissen ==")
    with tempfile.TemporaryDirectory() as tmp:
        saved = globals()["mask_secrets"]

        def exploding(_text, _pat):
            raise RuntimeError("Muster kaputt")

        globals()["mask_secrets"] = exploding
        try:
            rec = one(tmp, "x", "deny", "y",
                      {"tool_input": {"command": "secret AKIAIOSFODNN7EXAMPLE"}})
            check("werfende Maskierung wirft nicht nach aussen", True)
            check("werfende Maskierung schreibt KEINEN Klartext",
                  rec["target"] == "" and rec.get("mask_error") is True)
        except Exception as exc:
            check("werfende Maskierung wirft nicht nach aussen (%r)" % exc, False)
        finally:
            globals()["mask_secrets"] = saved

    print("\n== Fehlende Muster: Grenze faellt zurueck, kein Klartext-Zuwachs ==")
    saved_cache = list(_PATTERNS)
    del _PATTERNS[:]
    _PATTERNS.append(None)
    with tempfile.TemporaryDirectory() as tmp:
        rec = one(tmp, "x", "deny", "y",
                  {"tool_input": {"command": "echo " + "a" * 5000}})
        check("ohne secret_patterns gilt auch fuer Treffer die Grenze 1000",
              len(rec["target"]) == LEGACY_MAX_TARGET)
        check("der Ausfall steht im Eintrag", rec.get("mask") == "unavailable")
        check("die Kappung bleibt trotzdem markiert",
              rec.get("target_truncated") is True)
    del _PATTERNS[:]
    _PATTERNS.extend(saved_cache)

    print("\n== SSOT: Muster sind nicht von der Sweep-Quelle abgedriftet ==")
    sweep = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "commit-secret-sweep.py")
    pat = patterns()
    if not os.path.exists(sweep) or pat is None:
        check("commit-secret-sweep.py und secret_patterns.py gefunden", False)
    else:
        src = open(sweep, encoding="utf-8").read()
        norm = re.sub(r'"\s*r"', "", src)
        drift = [label for rx, label in pat.HIGH_CONF if rx.pattern not in norm]
        check("jedes HIGH_CONF-Muster steht wortgleich im Sweep (sonst Drift: %s)"
              % drift, not drift)
        check("GENERIC-Form und Schwellen identisch",
              r"[A-Za-z0-9]{%d,}" in src and "GENERIC_MIN_LEN = 24" in src
              and "GENERIC_MIN_ENTROPY = 3.8" in src
              and "GENERIC_MIN_TRANSITIONS = 3" in src)

    print("\n== Fail-open: kein Aufruf darf werfen ==")
    broken = [
        ("Verzeichnis nicht anlegbar", dict(log_dir="/dev/null/kein-verzeichnis")),
        ("Pfad ist eine Datei", dict(log_dir="/dev/null")),
    ]
    for name, kwargs in broken:
        try:
            log_decision("x", "deny", "y", {"tool_input": {"command": "z"}}, **kwargs)
            check(name + " wird verschluckt", True)
        except Exception as exc:
            check(name + " wird verschluckt (" + repr(exc) + ")", False)
    for name, payload in (("data ist None", None), ("data ist Liste", []),
                          ("tool_input ist kein dict", {"tool_input": 42}),
                          ("nicht serialisierbares Ziel", {"tool_input": {"command": object()}})):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                log_decision("x", "deny", "y", payload, log_dir=tmp)
                check(name + " wirft nicht", True)
            except Exception as exc:
                check(name + " wirft nicht (" + repr(exc) + ")", False)

    print("\n== Kill-Switch ==")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ[DISABLE_ENV] = "1"
        log_decision("x", "deny", "y", {"tool_input": {"command": "z"}}, log_dir=tmp)
        os.environ.pop(DISABLE_ENV, None)
        check("CLAUDE_GATE_LOG_DISABLE=1 schreibt nichts",
              not glob.glob(os.path.join(tmp, "*.jsonl")))

    print("\n== Rotation: Tagesdateien aelter als 90 Tage ==")
    with tempfile.TemporaryDirectory() as tmp:
        old = os.path.join(tmp, "2020-01-01.jsonl")
        keep = os.path.join(tmp, "2020-01-02.jsonl")
        fremd = os.path.join(tmp, "state.json")
        for victim in (old, keep, fremd):
            open(victim, "w").write("{}\n")
        stale = time.time() - 200 * 86400
        os.utime(old, (stale, stale))
        os.utime(fremd, (stale, stale))
        log_decision("x", "allow", "y", {"tool_input": {"command": "z"}}, log_dir=tmp)
        check("alte Tagesdatei geloescht", not os.path.exists(old))
        check("frische Tagesdatei bleibt", os.path.exists(keep))
        check("fremde Datei bleibt unangetastet (kein Glob auf alles)",
              os.path.exists(fremd))
        zweiter = os.path.join(tmp, "2019-01-01.jsonl")
        open(zweiter, "w").write("{}\n")
        os.utime(zweiter, (stale, stale))
        log_decision("x", "allow", "y", {"tool_input": {"command": "z"}}, log_dir=tmp)
        check("zweiter Aufruf am selben Tag prunt nicht erneut (ein listdir pro Tag)",
              os.path.exists(zweiter))

    print("\n== End-to-end gegen ein echtes Gate (eigener Prozess) ==")
    gate = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "destructive-command-gate.py")
    hit = json.dumps({"tool_input": {"command": "rm -rf /Users/alice/wichtig"},
                      "permission_mode": "bypassPermissions",
                      "session_id": "e2e", "cwd": "/Users/alice", "tool_name": "Bash"})
    miss = json.dumps({"tool_input": {"command": "echo hallo"},
                       "permission_mode": "bypassPermissions",
                       "session_id": "e2e", "cwd": "/Users/alice", "tool_name": "Bash"})

    def run_gate(stdin_text, target_dir):
        env = dict(os.environ)
        env[DIR_ENV] = target_dir
        env.pop(DISABLE_ENV, None)
        return subprocess.run([sys.executable, gate], input=stdin_text,
                              capture_output=True, text=True, env=env)

    def written_records(target_dir):
        recs = []
        for path in glob.glob(os.path.join(target_dir, "*.jsonl")):
            recs += [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
        return recs

    if not os.path.exists(gate):
        check("destructive-command-gate.py gefunden", False)
    else:
        with tempfile.TemporaryDirectory() as target:
            proc = run_gate(hit, target)
            out = json.loads(proc.stdout or "{}")
            decision = out.get("hookSpecificOutput", {}).get("permissionDecision")
            check("echtes Gate denyt weiterhin", decision == "deny")
            check("deny erzeugt eine Protokollzeile",
                  any(r["decision"] == "deny"
                      and r["gate"] == "destructive-command-gate"
                      and r["target"].startswith("rm -rf")
                      for r in written_records(target)))
            check("echtes Gate traegt origin=hook ein",
                  all(r.get("origin") == "hook" for r in written_records(target)))
        with tempfile.TemporaryDirectory() as target:
            proc = run_gate(miss, target)
            check("Durchlass gibt weiterhin keine Ausgabe", proc.stdout.strip() == "")
            check("allow erzeugt eine Protokollzeile",
                  any(r["decision"] == "allow" for r in written_records(target)))

        broken = "/dev/null/kein-verzeichnis"
        proc = run_gate(hit, broken)
        out = json.loads(proc.stdout or "{}")
        check("kaputtes Log-Ziel aendert die deny-Entscheidung nicht",
              out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny")
        check("kaputtes Log-Ziel laesst den Exit-Code unberuehrt",
              proc.returncode == 0)
        check("kaputtes Log-Ziel erzeugt keine Fehlerausgabe", proc.stderr.strip() == "")
        proc = run_gate(miss, broken)
        check("kaputtes Log-Ziel aendert den Durchlass nicht",
              proc.stdout.strip() == "" and proc.returncode == 0)

    total = len(failures)
    print("\nGesamt: OK" if not total else "\nGesamt: %d FEHLER" % total)
    raise SystemExit(1 if total else 0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest()
