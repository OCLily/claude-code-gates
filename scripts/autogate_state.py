#!/usr/bin/env python3
# State machine behind stop-gate.py. --mark records a plan file written under plans/ (PostToolUse Write);
# --clear removes it when a critic agent is dispatched with that path in its prompt (PostToolUse Agent).
# One state file per session with block counters, a session budget and a TTL; fails open on any error.
"""autogate_state.py — State-Machine hinter dem AUTOGATE (Plan -> critic).

Die Regel "nach einem Plan in /plans/ den critic Agent ausführen, BLOCKER = Stop"
bliebe ohne Durchsetzung ein reiner PostToolUse-additionalContext-Hinweis,
also Freiwilligkeit. Dieses Modul hält den State, `stop-gate.py` macht
daraus eine harte Sperre.

Rollen (beide lesen den Hook-Payload von stdin, schreiben nichts nach stdout):
- `--mark`  (PostToolUse Write): Plan-Pfad in die pending-Liste eintragen.
  Ergänzt einen additionalContext-Hinweis, ersetzt ihn nicht. Nur
  `Write` bewaffnet — `Edit` (Archivierung, Addenda, Tippfehler an
  bestehenden Plänen) bewaffnet bewusst nicht mehr.
- `--clear` (PostToolUse Agent): Agent-Dispatch mit subagent_type=critic, dessen
  Prompt einen pending-Pfad erwähnt -> genau diesen einen Pfad austragen.

Warum Unterstrich im Dateinamen (Abweichung von push-gate.py/prune-gate.py):
`stop-gate.py` importiert dieses Modul, damit das State-Schema genau eine
Quelle hat. Ein Bindestrich wäre nicht importierbar.

State-Schema v2: pro Session eine eigene Datei
`~/.claude/.autogate-pending.<session_id>.json`, siehe `state_path_for()`.
Das eliminiert den Cross-Session-Lost-Update strukturell statt ihn zu
beschreiben — zwei parallele Sessions schreiben nie mehr dieselbe Datei.

Bewusste Grenzen:
- `--clear` erkennt den DISPATCH, nicht garantiert den ABSCHLUSS. Seit Claude
  Code v2.1.198 laufen Subagents per Default im Hintergrund; der Agent-Tool-Call
  liefert dann `status: "async_launched"` zurück, bevor der critic fertig ist.
  Der Gate beweist also "critic mit diesem Plan im Prompt losgeschickt", nicht
  "critic hat geurteilt". Nur bei `status == "completed"` auszutragen wäre die
  schlechtere Fehlerrichtung: bei Hintergrund-Dispatches würde NIE ausgetragen,
  jeder Plan liefe in die Notbremse, der Gate wäre Dauerrauschen.
- Substring-Match auf den Dispatch-Prompt, kein Transcript-Parsing.
  Ein Prompt, der den Pfad nur zitiert ohne ihn prüfen zu lassen, trägt genauso
  aus. Layer, kein Zaun.
- Zwei Hooks können innerhalb DERSELBEN Session gleichzeitig schreiben
  (`--mark` / `--clear` / Stop, alle auf derselben Per-Session-Datei). Der
  Write ist atomar (os.replace), aber es gibt kein Lock — im Rennen kann ein
  Update verloren gehen. Folge: ein Block zu wenig oder einer zu viel, beides
  von der Notbremse gedeckt. Der Cross-Session-Fall ist durch die Per-Session-
  Datei strukturell ausgeschlossen (nicht mehr nur ein Race mit geringerer
  Wahrscheinlichkeit).
- Global, nicht auf ~/.claude gescoped: die AUTOGATE-Regel steht in der globalen
  Konfiguration und gilt in jedem Projekt mit einem /plans/-Verzeichnis. Die
  State-**Datei** ist trotzdem pro Session, nicht pro Projekt.
- Nur `.md` unter `/plans/` wird markiert, während ein additionalContext-Hinweis
  auf jeden `/plans/`-Pfad feuern kann. Die Sperre ist damit bewusst enger als
  der Hinweis, nie breiter.

Selbstheilung: Einträge verfallen nach TTL_SECONDS, jedes Lesen filtert das
State-File defensiv. Unlesbares oder kaputtes File = leerer State = kein
Block. Gilt auch für `budget`: TTL-Ablauf und ein `ts` in der Zukunft
werden identisch behandelt wie bei `pending`/`burned`-Einträgen.

KNOWN_GAPS (State-Layer-Anteil, Ergänzung zu stop-gate.py):
- Die State-Datei ist selbst ungeschützt. `rm ~/.claude/.autogate-pending.
  <sid>.json` oder ein direkter Write darauf (inkl. `budget` von Hand auf 0
  setzen oder `budget.ts` in die Zukunft schreiben) hebelt den State ohne
  Umweg über dieses Modul aus. Siehe stop-gate.py für die vollständige
  Begründung, warum das nicht zusätzlich geschützt wird.
- `--mark` ist umgehbar: ein Plan, der per Bash-Heredoc, `cp`, `mv` oder
  Editor außerhalb des Write-Tools entsteht, feuert den PostToolUse-Write-Hook
  nie und wird nie scharf.
- Bewusst fail-open bei kaputtem/fehlendem stdin-JSON und bei jedem
  Persist-Fehler (`save_verified()` liefert False -> kein Block): ein
  ungeschriebener/ungelesener State darf nie einen Turn blockieren, dieselbe
  Abwaegung wie in stop-gate.py.
"""
import glob
import json
import os
import re
import stat
import sys
import time

SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
BLOCK_LIMIT = 3
SESSION_BLOCK_LIMIT = 4
TTL_SECONDS = 12 * 3600


def state_path_for(session):
    """Injektive Whitelist statt Sanitizing: eine Zeichen-Ersetzung waere nicht
    injektiv (zwei IDs koennten auf denselben Dateinamen fallen -> Race wieder
    da) und oeffnet Pfad-Traversal ueber '/' und '..'. Akzeptierte IDs werden
    identisch abgebildet, alles andere -> None -> No-Op in allen drei Rollen."""
    if not isinstance(session, str) or not SESSION_RE.match(session):
        return None
    return os.path.expanduser(f"~/.claude/.autogate-pending.{session}.json")


def sweep(*, keep=None, now=None):
    """Minimaler Aufraeum-Sweep gegen unbegrenzte Akkumulation von Per-Session-
    Files, nur am Ende von --mark aufgerufen (seltenes Ereignis). lstat +
    S_ISREG schliesst Symlinks/Verzeichnisse aus; das Glob-Muster ist ein von
    uns allein besetzter Dotfile-Praefix. Fehler geschluckt (best effort)."""
    now = time.time() if now is None else now
    for candidate in glob.glob(os.path.expanduser("~/.claude/.autogate-pending.*.json")):
        if candidate == keep:
            continue
        try:
            st = os.lstat(candidate)
            if stat.S_ISREG(st.st_mode) and now - st.st_mtime > 2 * TTL_SECONDS:
                os.unlink(candidate)
        except OSError:
            pass


def _int(value):
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0  # negativ ist die gefaehrliche Richtung


def _entry_ok(entry, now):
    if not isinstance(entry, dict):
        return False
    path = entry.get("path")
    if not isinstance(path, str) or not path:
        return False
    try:
        ts = float(entry.get("ts") or 0)
    except (TypeError, ValueError):
        return False
    return 0 <= now - ts < TTL_SECONDS


def load(*, path, now=None):
    now = time.time() if now is None else now
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    state = {"version": 2, "pending": [], "burned": [],
             "budget": {"blocks": 0, "ts": now}}
    for key in ("pending", "burned"):
        items = raw.get(key)
        if isinstance(items, list):
            state[key] = [e for e in items if _entry_ok(e, now)]
    budget = raw.get("budget")
    if isinstance(budget, dict):
        try:
            ts = float(budget.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        blocks = _int(budget.get("blocks"))
        if blocks > 0 and 0 <= now - ts < TTL_SECONDS:
            state["budget"] = {"blocks": blocks, "ts": ts}
    return state


def save(state, *, path):
    """Fire-and-forget: fuer --mark/--clear, wo ein verlorener Schreibvorgang
    harmlos ist (naechster Lauf liest den vorherigen Stand erneut)."""
    tmp = f"{path}.tmp{os.getpid()}"
    try:
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def save_verified(state, *, path):
    """True nur, wenn ein anschliessendes Re-Read denselben State liefert.
    Fuer den Stop-Pfad: ein Block darf nur passieren, wenn der erhoehte
    Zaehler nachweislich persistiert ist."""
    tmp = f"{path}.tmp{os.getpid()}"
    try:
        with open(tmp, "w") as fh:
            json.dump(state, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        with open(path) as fh:
            return json.load(fh) == state
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def is_plan_path(file_path):
    return bool(file_path) and os.path.isabs(file_path) \
        and "/plans/" in file_path and file_path.endswith(".md")


def is_critic(subagent_type):
    name = (subagent_type or "").strip().lower()
    return name.rsplit(":", 1)[-1] == "critic"


def mention_forms(target):
    # Es gibt bewusst keine Kurzform (Suffix-Match wie "plans/foo.md"): eine
    # Kollisionspruefung saehe nur den State, nicht die Welt, und ein
    # gleichnamiger Plan in einem anderen Projekt truege faelschlich aus,
    # ohne dass der State selbst mehrdeutig waere. Die ~-Form wird erkannt: sie
    # expandiert zu genau einem absoluten Pfad, kann also nur falsch-positive
    # Blocks verhindern, nie welche ausloesen.
    forms = [target]
    home = os.path.expanduser("~")
    if target.startswith(home + "/"):
        forms.append("~/" + target[len(home) + 1:])
    return forms


def mentions(target, prompt):
    return any(form in prompt for form in mention_forms(target))


def mark_plan(file_path, session, *, path=None, now=None):
    now = time.time() if now is None else now
    if not is_plan_path(file_path):
        return None
    resolved = path or state_path_for(session)
    if not resolved:
        return None
    target = os.path.abspath(os.path.expanduser(file_path))
    state = load(path=resolved, now=now)
    same = [e for e in state["pending"] + state["burned"]
            if e["path"] == target and e.get("session") == session]
    if same:
        # Schon pending: Zaehler NICHT zuruecksetzen, sonst koennte ein Re-Edit
        # desselben Plans die Notbremse beliebig oft neu aufziehen.
        # Schon burned: in dieser Session nicht wieder scharf machen.
        return None
    state["pending"].append({"path": target, "session": session, "blocks": 0, "ts": now})
    save(state, path=resolved)
    return target


def clear_for_critic(subagent_type, prompt, session, *, path=None, now=None):
    now = time.time() if now is None else now
    if not is_critic(subagent_type) or not prompt:
        return []
    resolved = path or state_path_for(session)
    if not resolved:
        return []
    state = load(path=resolved, now=now)
    cleared, keep = [], []
    for entry in state["pending"]:
        if entry.get("session") == session and mentions(entry["path"], prompt):
            cleared.append(entry["path"])
        else:
            keep.append(entry)
    if cleared:
        state["pending"] = keep
        save(state, path=resolved)
    return cleared


def evaluate_stop(session, *, path=None, now=None):
    now = time.time() if now is None else now
    path = path or state_path_for(session)
    if not path:
        return {"blocking": [], "released": []}
    state = load(path=path, now=now)
    budget = state["budget"]["blocks"]
    blocking, released, keep = [], [], []
    for entry in state["pending"]:
        if entry.get("session") != session:
            keep.append(entry)
            continue
        blocks = _int(entry.get("blocks"))
        path_hit = blocks >= BLOCK_LIMIT
        budget_hit = budget >= SESSION_BLOCK_LIMIT
        if path_hit or budget_hit:
            reason = "path_limit" if path_hit and not budget_hit else (
                "session_budget" if budget_hit and not path_hit else "both")
            released.append(dict(entry, blocks=blocks, release_reason=reason))
            continue
        entry["blocks"] = blocks + 1
        blocking.append(dict(entry))
        keep.append(entry)
    if not blocking and not released:
        return {"blocking": [], "released": []}
    state["pending"] = keep
    for entry in released:
        state["burned"].append({"path": entry["path"], "session": session, "ts": now})
    if blocking:
        state["budget"] = {"blocks": budget + 1, "ts": now}
        if not save_verified(state, path=path):
            return {"blocking": [], "released": released}
        return {"blocking": blocking, "released": released}
    save_verified(state, path=path)  # Ergebnis egal: released ist sicher
    return {"blocking": [], "released": released}


PLAN_CASES = [
    ("/Users/alice/.claude/plans/foo.md", True),
    ("/Users/alice/code/project-x/plans/track-a.md", True),
    ("/Users/alice/.claude/plans/sub/foo.md", True),
    ("/Users/alice/.claude/plans/foo.png", False),
    ("/Users/alice/.claude/plansfoo.md", False),
    ("/Users/alice/.claude/skills/foo/SKILL.md", False),
    ("plans/foo.md", False),
    ("", False),
]

CRITIC_CASES = [
    ("critic", True),
    ("Critic", True),
    ("some-plugin:critic", True),
    ("contrarian", False),
    ("code-reviewer", False),
    ("", False),
]

MENTION_CASES = [
    ("/Users/alice/.claude/plans/foo.md",
     "Pruefe /Users/alice/.claude/plans/foo.md gegen die 6 Dimensionen", True),
    ("/Users/alice/.claude/plans/foo.md", "Pruefe ~/.claude/plans/foo.md", True),
    ("/Users/alice/.claude/plans/foo.md", "Pruefe plans/foo.md", False),
    ("/Users/alice/.claude/plans/foo.md", "Pruefe plans/bar.md", False),
    ("/Users/alice/.claude/plans/foo.md", "Pruefe den Plan", False),
]

SESSION_PATH_CASES = [
    ("abc-123", True),
    ("a", True),
    ("A_b-9", True),
    ("../../etc/passwd", False),
    ("a/b", False),
    ("", False),
    (None, False),
    ("x" * 128, True),
    ("x" * 129, False),
]


def _selftest():
    global save_verified
    failures = 0
    total = 0

    def check(label, condition):
        nonlocal failures, total
        total += 1
        failures += 0 if condition else 1
        print(f"{'PASS' if condition else 'FAIL'} {label}")

    print("== autogate_state: Einheiten ==")
    for value, expected in PLAN_CASES:
        check(f"is_plan_path({value!r}) -> {expected}", is_plan_path(value) == expected)
    for value, expected in CRITIC_CASES:
        check(f"is_critic({value!r}) -> {expected}", is_critic(value) == expected)
    for target, prompt, expected in MENTION_CASES:
        check(f"mentions(..., {prompt!r}) -> {expected}", mentions(target, prompt) == expected)
    for session, expected in SESSION_PATH_CASES:
        check(f"state_path_for({session!r}) valid -> {expected}",
              (state_path_for(session) is not None) == expected)
    a = state_path_for("session-a")
    b = state_path_for("session-b")
    check("state_path_for injective: 'session-a' != 'session-b'",
          a != b and a is not None and b is not None)

    print("\n== autogate_state: No-Op fuer abgelehnte session_id (alle drei Rollen) ==")
    bad = "../../etc/passwd"
    r1 = mark_plan("/Users/alice/.claude/plans/x.md", bad)
    r2 = clear_for_critic("critic", "/Users/alice/.claude/plans/x.md", bad)
    r3 = evaluate_stop(bad)
    check(f"mark_plan/clear_for_critic/evaluate_stop No-Op fuer {bad!r}",
          r1 is None and r2 == [] and r3 == {"blocking": [], "released": []})

    print("\n== autogate_state: load()/save_verified() Roundtrip ==")
    import shutil
    import tempfile
    root = tempfile.mkdtemp(prefix="autogate-state-selftest-")
    try:
        sp = os.path.join(root, "state.json")

        # budget von Hand geschrieben -> load() rekonstruiert es
        with open(sp, "w") as fh:
            json.dump({"version": 2, "pending": [], "burned": [],
                       "budget": {"blocks": 4, "ts": 1000.0}}, fh)
        loaded = load(path=sp, now=1000.0 + 10)
        check("load() rekonstruiert budget.blocks=4 von Hand geschrieben",
              loaded["budget"] == {"blocks": 4, "ts": 1000.0})

        # Budget-TTL: ts aelter als TTL_SECONDS -> zurueck auf 0
        loaded = load(path=sp, now=1000.0 + TTL_SECONDS + 1)
        check("load() Budget-TTL abgelaufen -> zurueck auf 0",
              loaded["budget"] == {"blocks": 0, "ts": 1000.0 + TTL_SECONDS + 1})

        # Zukunfts-ts-Guard
        with open(sp, "w") as fh:
            json.dump({"version": 2, "pending": [], "burned": [],
                       "budget": {"blocks": 2, "ts": 999999999999.0}}, fh)
        loaded = load(path=sp, now=1000.0)
        check("load() Budget mit ts in der Zukunft -> verworfen", loaded["budget"]["blocks"] == 0)

        # save_verified: Erfolgsfall
        vstate = {"version": 2, "pending": [], "burned": [], "budget": {"blocks": 1, "ts": 42.0}}
        check("save_verified() Normalfall -> True", save_verified(vstate, path=sp) is True)

        # save_verified: nicht schreibbares Verzeichnis -> False. (Datei-Permissions allein
        # (chmod 0444 auf die Zieldatei) bremsen os.replace() NICHT -- rename prueft nur die
        # Schreibrechte des Verzeichnisses, nicht die Bits der ersetzten Datei. Deshalb testen
        # wir hier ausschliesslich das Verzeichnis, nicht die Datei.)
        ro_dir = os.path.join(root, "readonly")
        os.mkdir(ro_dir)
        ro_path = os.path.join(ro_dir, "state.json")
        os.chmod(ro_dir, 0o555)
        try:
            ok = save_verified({"version": 2, "pending": [], "burned": [],
                                 "budget": {"blocks": 0, "ts": 0}}, path=ro_path) is False
        finally:
            os.chmod(ro_dir, 0o755)
        check("save_verified() Verzeichnis 0555 -> False", ok)

        # Auf evaluate_stop-Ebene: Verzeichnis 0555 -> kein Block
        blocked_dir = os.path.join(root, "blocked")
        os.mkdir(blocked_dir)
        bp = os.path.join(blocked_dir, "state.json")
        mark_plan("/Users/alice/.claude/plans/ro.md", "s-ro", path=bp)
        os.chmod(blocked_dir, 0o555)
        try:
            result = evaluate_stop("s-ro", path=bp)
        finally:
            os.chmod(blocked_dir, 0o755)
        check("evaluate_stop() Verzeichnis 0555 -> blocking == []", result["blocking"] == [])

        # save_verified gemockt False -> evaluate_stop blockt trotz frisch bewaffnetem Pfad nicht
        mp = os.path.join(root, "mocked.json")
        mark_plan("/Users/alice/.claude/plans/mocked.md", "s-mock", path=mp)
        real_save_verified = save_verified
        save_verified = lambda state, *, path: False
        try:
            result = evaluate_stop("s-mock", path=mp)
        finally:
            save_verified = real_save_verified
        check("evaluate_stop() save_verified() gemockt False -> blocking == []", result["blocking"] == [])

        # Session-Budget ueber echten save/load-Roundtrip, STAGGERED Ankunft: jeder der ersten
        # 3 Plaene wird einzeln scharf gemacht, einmal geblockt und sofort durch critic
        # ausgetragen (eigener Pfad-Zaehler bleibt bei 1, weit unter BLOCK_LIMIT). Erst danach
        # kommen zwei weitere Plaene, deren EIGENER Zaehler noch bei 0/1 steht, wenn das
        # Session-Budget bereits SESSION_BLOCK_LIMIT erreicht -- das ist der einzige Weg, den
        # session_budget-Freigabepfad unabhaengig vom path_limit-Pfad zu triggern.
        bsp = os.path.join(root, "budget.json")
        for i in range(3):
            p = f"/Users/alice/.claude/plans/budget-warm-{i}.md"
            mark_plan(p, "s-budget", path=bsp)
            result = evaluate_stop("s-budget", path=bsp)
            check(f"Warmup-Block #{i+1} fuer {p} loest blocking aus", bool(result["blocking"]))
            cleared = clear_for_critic("critic", f"critic-Check {p}", "s-budget", path=bsp)
            check(f"  critic traegt {p} sofort wieder aus", cleared == [p])
        check("Session-Budget nach 3 staggered Blocks == 3",
              load(path=bsp)["budget"]["blocks"] == 3)

        p3 = "/Users/alice/.claude/plans/budget-late-a.md"
        p4 = "/Users/alice/.claude/plans/budget-late-b.md"
        mark_plan(p3, "s-budget", path=bsp)
        result = evaluate_stop("s-budget", path=bsp)  # 4. Block treibt Budget 3 -> 4
        check("4. Block treibt Session-Budget auf SESSION_BLOCK_LIMIT (4)",
              bool(result["blocking"]) and load(path=bsp)["budget"]["blocks"] == SESSION_BLOCK_LIMIT)
        mark_plan(p4, "s-budget", path=bsp)  # frisch, eigener Zaehler = 0
        result = evaluate_stop("s-budget", path=bsp)
        check("Stop #5: Session-Budget-Notbremse zieht VOR dem Pfad-Limit (beide release_reason=session_budget)",
              result["blocking"] == [] and
              {e["path"] for e in result["released"]} == {p3, p4} and
              all(e.get("release_reason") == "session_budget" for e in result["released"]))

        # Bekanntes, ausdruecklich AKZEPTIERTES Verhalten: bei GLEICHZEITIG
        # ankommenden Plaenen laufen alle Pfad-Zaehler im Gleichschritt (jeder evaluate_stop-
        # Aufruf inkrementiert ALLE pending-Eintraege der Session gemeinsam) --
        # die Notbremse zieht deshalb ueber BLOCK_LIMIT (3 Blocks), BEVOR das Session-Budget (4)
        # ueberhaupt erreicht werden kann. Hier deterministisch dokumentiert,
        # nicht stillschweigend anders erwartet.
        wsp = os.path.join(root, "wave.json")
        wave_plans = [f"/Users/alice/.claude/plans/wave-{i}.md" for i in range(5)]
        for p in wave_plans:
            mark_plan(p, "s-wave", path=wsp)
        blocks_seen = 0
        for _ in range(BLOCK_LIMIT):
            result = evaluate_stop("s-wave", path=wsp)
            if result["blocking"]:
                blocks_seen += 1
        final = evaluate_stop("s-wave", path=wsp)
        check(f"akzeptiert: gleichzeitige Welle aus {len(wave_plans)} Plaenen loest "
              f"path_limit-Freigabe nach genau {BLOCK_LIMIT} Blocks aus, Budget erreicht nie "
              f"{SESSION_BLOCK_LIMIT}",
              blocks_seen == BLOCK_LIMIT and final["blocking"] == [] and
              len(final["released"]) == len(wave_plans) and
              all(e.get("release_reason") == "path_limit" for e in final["released"]) and
              load(path=wsp)["budget"]["blocks"] == BLOCK_LIMIT)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print(f"\n{total - failures}/{total} OK")
    sys.exit(1 if failures else 0)


def main():
    role = sys.argv[1] if len(sys.argv) > 1 else ""
    if role == "--selftest":
        _selftest()
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    session = data.get("session_id") or ""
    tool_input = data.get("tool_input") or {}
    try:
        if role == "--mark":
            mark_plan(tool_input.get("file_path") or "", session)
            sweep(keep=state_path_for(session))
        elif role == "--clear":
            clear_for_critic(tool_input.get("subagent_type") or "",
                             tool_input.get("prompt") or "", session)
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
