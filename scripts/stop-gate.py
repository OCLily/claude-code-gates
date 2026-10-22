#!/usr/bin/env python3
# Stop hook: blocks ending a turn while a plan file written under plans/ has no critic-agent dispatch
# recorded for it (per-session state in autogate_state.py). Shadow mode by default: the decision only
# goes to a shadow log; it blocks only with AUTOGATE_STOP_LIVE=1, and an emergency brake releases it.
"""stop-gate.py — Stop-Hook: macht das AUTOGATE (Plan -> critic) verbindlich.

Ohne Durchsetzung ist die Regel "nach einem Plan in /plans/ den critic Agent
ausfuehren, BLOCKER = Stop" ein reiner PostToolUse-additionalContext-Hinweis. Dieser
Hook blockt das Turn-Ende, solange für die laufende Session ein Plan pending
ist. State und State-Schema: `autogate_state.py` (SSOT, Per-Session-State-File).

SHADOW-MODUS (Standard — NICHT VERHANDELBAR):
`run()` wertet `build_output()` bei JEDEM Stop-Aufruf vollständig aus — der
State wird real mutiert (Zähler/Budget), damit das Verhalten in der Shadow-
Phase realistisch beobachtbar ist — aber das Ergebnis geht standardmäßig NUR
in `~/.claude/.autogate-shadow.log`, niemals an Claude Code. Nur wenn
`AUTOGATE_STOP_LIVE=1` im Environment gesetzt ist, geht die echte Entscheidung
(inkl. `decision:"block"`) tatsächlich raus. Diese Var wird beim Standard-Wiring
NIRGENDS gesetzt (weder settings.json noch sonstwo) — `decision:block`
ist damit strukturell unerreichbar, bis jemand das Environment ändert.

Mode-Whitelist — umgekehrt zu den anderen Gates (z.B. prune-gate.py /
push-gate.py), deshalb explizit:
  BLOCKING_MODES = ("acceptEdits", "auto", "dontAsk", "bypassPermissions")
  mode not in BLOCKING_MODES -> KEIN Block (default, plan, fehlend, leer,
  jeder unbekannte künftige Modus).
Bewusste Abweichung von der Fail-closed-Konvention der anderen Gates: dort
kostet fail-closed einen abgelehnten Tool-Call, hier eine Turn-Blockade in
einer Umgebung, die wir nicht kennen. Andere Fehler-Ökonomie -> anderer
Default. Die Hooks-Dokumentation von Claude Code enumeriert alle sechs gültigen
Modi vollständig, die Whitelist ist damit verifizierbar abschließend.

NOTBREMSE (die eigentliche Sicherheitsanforderung, wichtiger als die
Durchsetzung selbst): pro pending-Pfad zählt `blocks`. Nach BLOCK_LIMIT (3)
ausgelösten Blocks wird der Eintrag beim nächsten Stop automatisch ausgetragen,
das Turn-Ende zugelassen und eine laute Warnung ausgegeben. Zusätzlich deckelt
`budget` (Session-weit, siehe autogate_state.py) die GESAMTZAHL an Blocks in
einer Session auf SESSION_BLOCK_LIMIT (4), unabhängig von der Pfad-Buchführung
— das verhindert den Zyklus "Plan schreiben -> Block -> neuen Plan schreiben ->
Block" beliebig oft. Vier Schichten verhindern ein Hängen von Claude Code:
1. eigener Zähler pro Pfad (BLOCK_LIMIT = 3),
2. Session-Budget (SESSION_BLOCK_LIMIT = 4, deckt auch mehrere Pläne),
3. `burned`-Liste: ein von der Notbremse freigegebener Pfad wird in derselben
   Session durch erneutes Schreiben NICHT wieder scharf,
4. Plattform-Notbremse: Claude Code übersteuert den Hook und beendet den Turn
   nach 8 aufeinanderfolgenden Blocks (Claude Code 2.1.220 und Hooks-
   Dokumentation) — GÜLTIG UNTER DEM DEFAULT-ENVIRONMENT, per
   `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP=0` abschaltbar. Deshalb sind Schichten 1-3
   nicht verhandelbar; sie tragen auch ohne Schicht 4.
Deckel-Ordnung: BLOCK_LIMIT 3 <= SESSION_BLOCK_LIMIT 4 < Plattform-Cap 8.
Es wird höchstens EINMAL pro Stop-Aufruf geblockt, auch wenn mehrere Pläne
pending sind (alle betroffenen Zähler steigen gemeinsam).

Warum die Notbremsen-Warnung in `systemMessage` und nicht im `reason` steht:
`reason` existiert laut Hooks-Dokumentation nur zusammen mit `decision: "block"` — genau
das, was die Notbremse verhindern soll. `systemMessage` ist der einzige Kanal,
der den Maintainer erreicht, ohne den Turn weiterlaufen zu lassen (`additionalContext`
scheidet aus: es setzt die Konversation ebenfalls fort). Die Blocks davor
tragen den Countdown ("Block 2 von 3") im reason-Text.

Fail-open in jede Richtung: Import-Fehler, kaputter Payload, Exception im
State, fehlgeschlagener Persist (`save_verified()` -> False), nicht-
whitelisted `session_id` (siehe autogate_state.py) -> exit 0 / kein Block.
Not-Aus für den Maintainer: `AUTOGATE_STOP_OFF=1` im Environment (deaktiviert auch das
Shadow-Logging vollständig — echtes Aus, nicht nur Blockieren).

Grenze: der Gate erkennt einen critic-DISPATCH mit dem Plan-Pfad im Prompt,
nicht zwingend dessen Abschluss (Begründung im Docstring von
autogate_state.py). Ein Layer, kein Zaun.

KNOWN_GAPS (vollständige Liste, State-Layer-Anteil in autogate_state.py):
- Dispatch statt Abschluss (s.o., Hintergrund-Subagents seit v2.1.198).
- Substring-Match: ein Prompt, der den Pfad nur zitiert, trägt genauso aus.
- `Edit` bewaffnet nicht mehr (nur `Write`) — Archivierungs-Edits an
  bestehenden Plänen bleiben unentdeckt, bewusst.
- Intra-Session-Race ohne Lock zwischen --mark/--clear/Stop auf derselben
  Datei (Cross-Session ist durch die Per-Session-Datei strukturell weg).
- Globale Geltung, nicht auf ein Projekt gescoped.
- Ein Layer, kein Zaun — kein Ersatz für tatsächliches Lesen des critic-Outputs.
- Die State-Datei ist selbst ungeschützt: `rm`/direkter Write auf
  `~/.claude/.autogate-pending.<sid>.json`, inklusive `budget` von Hand auf 0
  setzen oder `budget.ts` in die Zukunft schreiben, hebelt den Mechanismus
  vollständig aus. Bewusst nicht zusätzlich geschützt (kein chattr/Hash/
  Signatur) — Scope-Creep gegenüber "Layer, kein Zaun", und die Notbremsen-
  Warnung nennt den Pfad ohnehin explizit (der Maintainer braucht den Pointer zur
  Diagnose mehr, als Security-by-Obscurity hier wert wäre).
- `--mark` ist umgehbar: ein Plan, der per Bash-Heredoc, `cp`, `mv` oder Editor
  außerhalb des Write-Tools entsteht, feuert den Hook nie, wird nie scharf.
- `stop_hook_active` wird bewusst ignoriert. Es bei `true` als Nicht-Block zu
  werten, würde den Countdown auf genau einen Block kappen und das
  Notbremsen-Design aushebeln.
- Bewusst fail-open bei kaputtem/fehlendem stdin-JSON (except: sys.exit(0))
  UND bei unbekanntem/fehlendem `permission_mode`: ein Turn-Block ist ein
  deutlich teureres Fehlausschlagen als ein ausbleibender Block, deshalb
  fail-open statt fail-closed — anders gewichtet als die Write/Edit-Gates
  (dort ist ein zusaetzlicher deny billig).
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import autogate_state as state
except Exception:
    state = None

BLOCKING_MODES = ("acceptEdits", "auto", "dontAsk", "bypassPermissions")
KILL_SWITCH = "AUTOGATE_STOP_OFF"
LIVE_SWITCH = "AUTOGATE_STOP_LIVE"
SHADOW_LOG_PATH = os.path.expanduser("~/.claude/.autogate-shadow.log")


def _reason(entries):
    lines = "\n".join(f"  - {e['path']}  (Block {e['blocks']} von {state.BLOCK_LIMIT})"
                      for e in entries)
    return (
        "[AUTOGATE] Turn-Ende geblockt: fuer diese Plaene wurde kein critic-Dispatch "
        "mit dem Plan-Pfad im Prompt gesehen:\n" + lines + "\n"
        "Regel: nach einem Plan in /plans/ den critic Agent ausfuehren, "
        "BLOCKER-Befund = Stop, kein Weiter ohne explizites Go vom Maintainer. Dispatche JETZT "
        "den critic und nenne den Plan mit ABSOLUTEM Pfad oder in ~-Form im Prompt — nur "
        "daran erkennt der Gate den Durchlauf, \"plans/foo.md\" allein zaehlt nicht. Nach "
        f"{state.BLOCK_LIMIT} Blocks greift die Notbremse und die Sperre schaltet sich fuer "
        "diesen Plan ab."
    )


def _warning(entries, path):
    path_limit = [e["path"] for e in entries if e.get("release_reason") in ("path_limit", "both")]
    budget_hit = [e["path"] for e in entries if e.get("release_reason") == "session_budget"]
    parts = ["!!! AUTOGATE-NOTBREMSE GEZOGEN !!!"]
    if path_limit:
        parts.append(
            f"{', '.join(path_limit)} wurde {state.BLOCK_LIMIT}x blockiert, ohne dass ein "
            "critic-Dispatch mit diesem Pfad erkannt wurde.")
    if budget_hit:
        parts.append(
            f"Session-Budget erreicht ({state.SESSION_BLOCK_LIMIT} Blocks in dieser Session) — "
            f"freigegeben, obwohl der Pfad-Zaehler selbst das Limit noch nicht erreicht hatte: "
            f"{', '.join(budget_hit)}.")
    plural = "diese Plaene" if len(entries) > 1 else "diesen Plan"
    parts.append(
        f"Die Sperre ist fuer {plural} JETZT DEAKTIVIERT, das Turn-Ende wird zugelassen. Bitte "
        f"manuell pruefen: lief der critic wirklich, und gab es einen BLOCKER-Befund? "
        f"(State: {path})")
    return " ".join(parts)


def build_output(data, *, state_path=None, now=None):
    """Reine Entscheidungslogik. Mutiert State ueber evaluate_stop() (Zaehler/
    Budget werden bei einem Block real erhoeht und verifiziert persistiert),
    entscheidet aber NICHT ob das Ergebnis Claude Code erreicht — das regelt
    ausschliesslich run() ueber den Shadow/Live-Schalter."""
    if os.environ.get(KILL_SWITCH):
        return {}
    if data.get("permission_mode") not in BLOCKING_MODES:
        return {}
    session = data.get("session_id") or ""
    if not session:
        return {}
    path = state_path or state.state_path_for(session)
    if not path:
        return {}
    result = state.evaluate_stop(session, path=path, now=now)
    out = {}
    if result["released"]:
        out["systemMessage"] = _warning(result["released"], path)
    if result["blocking"]:
        out["decision"] = "block"
        out["reason"] = _reason(result["blocking"])
    return out


def _shadow_entry(data, hypothetical, *, now=None):
    return {
        "ts": now if now is not None else time.time(),
        "session_id": data.get("session_id"),
        "permission_mode": data.get("permission_mode"),
        "hook_event": data.get("hook_event_name"),
        "agent_id": data.get("agent_id"),
        "stop_hook_active": data.get("stop_hook_active"),
        "hypothetical_decision": hypothetical,
        "live": os.environ.get(LIVE_SWITCH) == "1",
    }


def _write_shadow_log(entry, *, log_path=None):
    log_path = log_path or SHADOW_LOG_PATH
    try:
        with open(log_path, "a") as fh:
            fh.write(json.dumps(entry, ensure_ascii=True) + "\n")
    except Exception:
        pass


def run(data, *, state_path=None, now=None, shadow_log_path=None):
    """Shadow/Live-Gate um build_output() (NICHT VERHANDELBAR).
    build_output() wird bei aktivem KILL_SWITCH gar nicht erst aufgerufen —
    Kill-Switch ist ein echtes Aus, auch fuers Shadow-Logging. Sonst: IMMER
    auswerten und loggen; nur bei AUTOGATE_STOP_LIVE=1 geht die echte
    Entscheidung an Claude Code raus."""
    if os.environ.get(KILL_SWITCH):
        return {}
    hypothetical = build_output(data, state_path=state_path, now=now)
    _write_shadow_log(_shadow_entry(data, hypothetical, now=now), log_path=shadow_log_path)
    if os.environ.get(LIVE_SWITCH) == "1":
        return hypothetical
    return {}


def _payload(mode="bypassPermissions", session="s-1"):
    return {"hook_event_name": "Stop", "permission_mode": mode,
            "session_id": session, "stop_hook_active": False}


def selftest():
    import glob
    import shutil
    import tempfile
    if state is None:
        print("FAIL Import von autogate_state fehlgeschlagen")
        sys.exit(1)

    failures = 0

    def check(label, condition):
        nonlocal failures
        failures += 0 if condition else 1
        print(f"{'PASS' if condition else 'FAIL'} {label}")

    check("Deckel-Ordnung BLOCK_LIMIT <= SESSION_BLOCK_LIMIT < 8",
          state.BLOCK_LIMIT <= state.SESSION_BLOCK_LIMIT < 8)

    root = tempfile.mkdtemp(prefix="autogate-selftest-")
    before_glob = sorted(glob.glob(os.path.expanduser("~/.claude/.autogate-pending.*.json")))
    sp = os.path.join(root, "state.json")
    plan_a = "/Users/alice/.claude/plans/alpha.md"
    plan_b = "/Users/alice/.claude/plans/beta.md"
    plan_c = "/Users/alice/.claude/plans/gamma.md"
    try:
        print("== 1) Happy Path: Plan -> pending -> critic -> leer -> kein Block ==")
        state.mark_plan(plan_a, "s-1", path=sp)
        pending = [e["path"] for e in state.load(path=sp)["pending"]]
        check(f"nach Plan-Write pending = {pending}", pending == [plan_a])
        cleared = state.clear_for_critic(
            "critic", f"Pruefe den Plan {plan_a} gegen die 6 Dimensionen.", "s-1", path=sp)
        check(f"critic-Dispatch traegt {cleared} aus", cleared == [plan_a])
        check("pending danach leer", state.load(path=sp)["pending"] == [])
        out = build_output(_payload(), state_path=sp)
        check("Stop liefert keinen Block", out == {})

        print("\n== 2) Notbremse ueber Pfad-Zaehler: 3x Block ohne critic -> Freigabe ==")
        state.mark_plan(plan_b, "s-1", path=sp)
        for expected_n in (1, 2, 3):
            out = build_output(_payload(), state_path=sp)
            ok = out.get("decision") == "block" and f"Block {expected_n} von 3" in out.get("reason", "")
            check(f"Stop #{expected_n} blockt, Countdown 'Block {expected_n} von 3' im reason", ok)
            check(f"  Zaehler im State = {expected_n}",
                  [e["blocks"] for e in state.load(path=sp)["pending"]] == [expected_n])
        out = build_output(_payload(), state_path=sp)
        check("Stop #4 blockt NICHT mehr (Notbremse)", "decision" not in out)
        check("Warnung enthaelt 'AUTOGATE-NOTBREMSE GEZOGEN'",
              "AUTOGATE-NOTBREMSE GEZOGEN" in out.get("systemMessage", ""))
        check("Warnung nennt den Plan-Pfad", plan_b in out.get("systemMessage", ""))
        check("Warnung nennt die echte Per-Session-Datei",
              sp in out.get("systemMessage", ""))
        check("Freigabe ueber Pfad-Zaehler NENNT NICHT 'Session-Budget' (war path_limit)",
              "Session-Budget" not in out.get("systemMessage", ""))
        after = state.load(path=sp)
        check("pending nach Notbremse leer", after["pending"] == [])
        check("Pfad steht auf der burned-Liste", [e["path"] for e in after["burned"]] == [plan_b])
        print("    systemMessage: " + out.get("systemMessage", ""))
        state.mark_plan(plan_b, "s-1", path=sp)
        check("erneutes Schreiben desselben Plans macht ihn nicht wieder scharf",
              state.load(path=sp)["pending"] == [])
        check("und der naechste Stop laeuft durch", build_output(_payload(), state_path=sp) == {})

        print("\n== 3) Notbremse ueber Session-Budget (staggered Ankunft) ==")
        # Gleichzeitig ankommende Plaene laufen im Gleichschritt -> die Notbremse zieht dann
        # ueber BLOCK_LIMIT (3), BEVOR SESSION_BLOCK_LIMIT (4) erreichbar ist (akzeptiertes
        # Verhalten, siehe autogate_state.py --selftest fuer die deterministische Demonstration).
        # Um den session_budget-Freigabepfad hier zu isolieren, kommen die ersten 3 Plaene
        # NACHEINANDER an und werden sofort durch critic ausgetragen (eigener Zaehler bleibt
        # bei 1), erst die letzten beiden bleiben stehen, wenn das Budget bei 4 ankommt.
        bsp = os.path.join(root, "budget.json")
        for i in range(3):
            p = f"/Users/alice/.claude/plans/budget-warm-{i}.md"
            state.mark_plan(p, "s-budget", path=bsp)
            out = build_output(_payload(session="s-budget"), state_path=bsp)
            check(f"Warmup-Stop #{i+1} blockt", out.get("decision") == "block")
            cleared = state.clear_for_critic("critic", f"critic-Check {p}", "s-budget", path=bsp)
            check(f"  critic traegt {p} sofort wieder aus", cleared == [p])
        check("budget.blocks == 3 nach 3 staggered Blocks",
              state.load(path=bsp)["budget"]["blocks"] == 3)
        p3 = "/Users/alice/.claude/plans/budget-late-a.md"
        p4 = "/Users/alice/.claude/plans/budget-late-b.md"
        state.mark_plan(p3, "s-budget", path=bsp)
        out = build_output(_payload(session="s-budget"), state_path=bsp)
        check("4. Block treibt budget.blocks auf SESSION_BLOCK_LIMIT (4)",
              out.get("decision") == "block" and
              state.load(path=bsp)["budget"]["blocks"] == state.SESSION_BLOCK_LIMIT)
        state.mark_plan(p4, "s-budget", path=bsp)  # frisch, eigener Zaehler = 0
        out = build_output(_payload(session="s-budget"), state_path=bsp)
        check("Stop #5 blockt NICHT mehr (Session-Budget-Notbremse zieht vor dem Pfad-Limit)",
              "decision" not in out)
        check("Warnung nennt 'Session-Budget'", "Session-Budget" in out.get("systemMessage", ""))
        remaining = [e["path"] for e in state.load(path=bsp)["pending"]]
        check("beide zuletzt gemarkten Plaene mitfreigegeben, obwohl ihr eigener Zaehler < 3 war",
              remaining == [])

        print("\n== 4) Zwei Plaene pending, critic nennt nur einen ==")
        state.mark_plan(plan_a, "s-2", path=sp)
        state.mark_plan(plan_c, "s-2", path=sp)
        check("beide pending",
              sorted(e["path"] for e in state.load(path=sp)["pending"]) == sorted([plan_a, plan_c]))
        cleared = state.clear_for_critic("critic", f"critic-Auftrag zu {plan_a}", "s-2", path=sp)
        check(f"nur {plan_a} ausgetragen", cleared == [plan_a])
        check(f"{plan_c} bleibt pending",
              [e["path"] for e in state.load(path=sp)["pending"]] == [plan_c])
        out = build_output(_payload(session="s-2"), state_path=sp)
        check("Stop blockt weiterhin", out.get("decision") == "block")
        check("reason nennt nur den offenen Plan",
              plan_c in out.get("reason", "") and plan_a not in out.get("reason", ""))
        check("reason fordert absoluten Pfad / ~-Form explizit",
              "ABSOLUTEM Pfad" in out.get("reason", "") and "~-Form" in out.get("reason", ""))

        print("\n== 5) Randfaelle ==")
        for mode in ("default", "plan", "voellig-neuer-modus", "", None):
            before = [e["blocks"] for e in state.load(path=sp)["pending"]]
            payload = _payload(session="s-2")
            if mode is None:
                del payload["permission_mode"]
            else:
                payload["permission_mode"] = mode
            out = build_output(payload, state_path=sp)
            check(f"Mode {mode!r}: kein Block (Whitelist), Zaehler unveraendert",
                  out == {} and before == [e["blocks"] for e in state.load(path=sp)["pending"]])
        # Eigene, frische Datei fuer die Mode-Checks: `sp` hat durch Test 2+4 bereits ein
        # Session-Budget von 4 angesammelt (jeweils session-uebergreifend via state_path=sp
        # erzwungen) -- ein frischer Pfad haelt den Test unabhaengig davon, dass hier ausschliesslich
        # der Mode-Whitelist-Zweig geprueft werden soll, nicht die Notbremse.
        msp = os.path.join(root, "modes.json")
        for mode in BLOCKING_MODES:
            fresh = f"/Users/alice/.claude/plans/mode-{mode}.md"
            state.mark_plan(fresh, f"s-mode-{mode}", path=msp)
            out = build_output(_payload(mode=mode, session=f"s-mode-{mode}"), state_path=msp)
            check(f"Mode {mode!r} in BLOCKING_MODES: blockt", out.get("decision") == "block")
        out = build_output(_payload(session="fremde-session"), state_path=sp)
        check("fremde Session sieht das pending einer anderen Session nicht", out == {})
        out = build_output(_payload(session="../../etc/passwd"), state_path=None)
        check("nicht-whitelisted session_id -> No-Op (kein state_path override)", out == {})
        os.environ[KILL_SWITCH] = "1"
        out = build_output(_payload(session="s-2"), state_path=sp)
        del os.environ[KILL_SWITCH]
        check(f"Not-Aus {KILL_SWITCH}=1 schaltet den Gate ab", out == {})
        with open(sp, "w") as fh:
            fh.write("{kaputt")
        check("kaputtes State-File -> leerer State -> kein Block",
              build_output(_payload(session="s-2"), state_path=sp) == {})
        check("fehlendes State-File -> kein Block",
              build_output(_payload(), state_path=os.path.join(root, "gibtsnicht.json")) == {})
        state.mark_plan(plan_a, "s-3", path=sp, now=1000.0)
        check("Eintrag aelter als TTL verfaellt",
              state.load(path=sp, now=1000.0 + state.TTL_SECONDS + 1)["pending"] == [])
        state.mark_plan(plan_a, "s-9", path=sp)
        check("Nicht-critic-Agent traegt nichts aus",
              state.clear_for_critic("code-reviewer", plan_a, "s-9", path=sp) == [])
        check("critic ohne Pfad im Prompt traegt nichts aus",
              state.clear_for_critic("critic", "Pruef mal den Plan", "s-9", path=sp) == [])

        print("\n== 6) Shadow/Live-Gate (run()) ==")
        with open(sp, "w") as fh:
            json.dump({"version": 2, "pending": [], "burned": [], "budget": {"blocks": 0, "ts": 0}}, fh)
        state.mark_plan(plan_a, "s-shadow", path=sp)
        for _ in range(state.BLOCK_LIMIT - 1):
            state.evaluate_stop("s-shadow", path=sp)  # Zaehler knapp UNTER das Blocklimit treiben,
            # damit der naechste (der zu testende) Aufruf noch ein echter Block ist, keine Freigabe
        shadow_log = os.path.join(root, "shadow.log")
        out = run(_payload(session="s-shadow"), state_path=sp, shadow_log_path=shadow_log)
        check("run() ohne AUTOGATE_STOP_LIVE gibt NIEMALS decision:block zurueck",
              "decision" not in out)
        check("run() schreibt trotzdem in den Shadow-Log", os.path.exists(shadow_log))
        with open(shadow_log) as fh:
            logged = [json.loads(line) for line in fh if line.strip()]
        check("Shadow-Log enthaelt genau 1 Eintrag mit hypothetical_decision.decision=='block'",
              len(logged) == 1 and logged[0]["hypothetical_decision"].get("decision") == "block")
        check("Shadow-Log-Eintrag markiert live=False", logged[0]["live"] is False)
        os.environ[LIVE_SWITCH] = "1"
        try:
            out_live = run(_payload(session="s-shadow"), state_path=sp, shadow_log_path=shadow_log)
        finally:
            del os.environ[LIVE_SWITCH]
        check("run() MIT AUTOGATE_STOP_LIVE=1 gibt die echte Notbremsen-Freigabe zurueck",
              "systemMessage" in out_live)
        os.environ[KILL_SWITCH] = "1"
        try:
            out_off = run(_payload(session="s-shadow"), state_path=sp, shadow_log_path=shadow_log)
        finally:
            del os.environ[KILL_SWITCH]
        with open(shadow_log) as fh:
            logged_after_off = [json.loads(line) for line in fh if line.strip()]
        check("Kill-Switch schaltet auch das Shadow-Logging ab (kein neuer Eintrag)",
              out_off == {} and len(logged_after_off) == 2)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\n== Sicherheitszusicherung ==")
    after_glob = sorted(glob.glob(os.path.expanduser("~/.claude/.autogate-pending.*.json")))
    check("keine echten Per-Session-State-Files vom Selftest angelegt/veraendert (Glob vorher==nachher)",
          before_glob == after_glob)
    print("(Kein echter Stop-Block ausgeloest — nur build_output()/run() gegen Temp-Dateien.)")
    print(f"\nErgebnis: {'ALLE OK' if not failures else str(failures) + ' FEHLER'}")
    sys.exit(1 if failures else 0)


# Entscheidungs-Protokoll (metrics/gates). Fail-open und bewusst defensiv
# importiert: ein fehlender oder kaputter Logger darf dieses Gate nie zum
# Absturz bringen -- ein abgestuerzter PreToolUse-Hook sperrt im schlimmsten
# Fall jeden Bash-Aufruf aus.
GATE_LOG_NAME = "stop-gate"
# Der Selftest darf die Messung nicht verschmutzen -- er faehrt praeparierte
# Payloads durch dieselbe Datei, teils als Subprozess (erbt das Env).
if "--selftest" in sys.argv:
    os.environ["CLAUDE_GATE_LOG_DISABLE"] = "1"
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
try:
    from gate_log import log_decision
except Exception:
    def log_decision(*_args, **_kwargs):
        pass


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
    if state is None:
        sys.exit(0)
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    try:
        out = run(data)
    except Exception:
        sys.exit(0)
    log_decision(GATE_LOG_NAME,
                 "deny" if (out or {}).get("decision") == "block" else "allow",
                 "plan-critic-loop", data, target="")
    if out:
        print(json.dumps(out, ensure_ascii=True))
    sys.exit(0)


if __name__ == "__main__":
    main()
