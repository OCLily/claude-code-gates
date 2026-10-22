#!/usr/bin/env python3
# PreToolUse hook for Bash: always denies `git add -A/--all/./*` and `git commit -a/--all`.
# It tokenizes the command per shell segment instead of matching one big regex, so words inside
# a quoted commit message do not trigger it. One decision (deny), not mode-aware; gaps are in KNOWN_GAPS.
"""git-add-commit-gate.py — PreToolUse Bash-Gate gegen `git add -A/--all/./*`
und `git commit -a/--all`.

Der Gate tokenisiert den Kommandotext statt ihn mit einem Ganzstring-Regex zu
matchen. Ein Einzeiler der Form `jq | tr '\\n' ' ' | grep -qE '<ein Riesenregex>'`
matcht gegen den KOMPLETTEN, zeilenweise geflatteten Kommandotext — nicht
gegen die tatsaechlichen git-Subcommand-Argumente. `grep -E` kennt keine
Shell-Quotes: ein Wort innerhalb von `-m "..."` ist fuer den Regex nicht von
einem echten Flag zu unterscheiden. Zwei False-Positive-Klassen eines
solchen Ganzstring-Regex (Repro in CASES unten):

1. Mehrzeilige Bash-Commands (z.B. `git add file.py` und `git commit -m "..."`
   auf getrennten Zeilen statt per `;`/`&&` verknuepft) werden von `tr '\\n'
   ' '' OHNE Trennzeichen zwischen den Statements geflattened — ein ADD-Zweig
   `add ... (-A|--all|\\.|\\*)` kann dadurch quer in die zweite Zeile matchen,
   sobald deren Commit-Message ein bares "*" enthaelt (z.B. eine Markdown-
   Bullet-Liste im Commit-Body).
2. Ein COMMIT-Zweig `-([a-z]*a[a-z]*|-all)` matcht JEDES Leerzeichen-
   getrennte "Wort" das mit "-" beginnt und ein "a" enthaelt, irgendwo im
   String — auch mitten in einer gequoteten -m-Message ("-parallel", "-fast",
   "-data" als Teil des Fliesstexts loesen faelschlich aus).

Loesung: echtes Tokenizing statt Ganzstring-Regex. Segmentierung exakt nach dem
SPLIT_RE-Muster der Geschwister-Gates (`;`/`&&`/`||`/`|`/Newline trennen
Statements), dann `shlex.split` pro Segment. Ein gequotetes -m-Argument wird
dadurch INNERHALB seines Segments zu einem einzigen Token — Woerter darin
koennen dort nie mehr als eigenstaendige Flag-Tokens matchen, weil nur ganze
Tokens auf Flag-Form geprueft werden (kein Teilstring-Match mehr). Enthaelt
die Message selbst eines der SPLIT_RE-Trennzeichen (`;`/`&&`/`||`/`|`/
Newline), splittet SPLIT_RE trotzdem mitten hinein, BEVOR shlex laeuft — der
darauf folgende `shlex.split` wirft dann meist ValueError (unbalanciertes
Quote) und faellt auf simples `.split()` zurueck, was in seltenen Faellen zu
False Positives fuehrt (Fail-closed, kein Schutzverlust, aber die
"IMMER ein Token"-Garantie gilt nur pro Segment, nicht ueber Trennzeichen in
Quotes hinweg).

Value-nehmende commit-Flags (-m/--message, -F/--file, -c/--reedit-message,
-C/--reuse-message, -t/--template, --author, --date, --cleanup, --fixup,
--squash, --trailer) konsumieren ihren separat uebergebenen Wert-Token. Ihre
ANGEHAENGTE Kurzform (`-mAlpha`, `-m"message mit a"`) wird ebenfalls als
Value erkannt (Praefix-Check auf `-m`/`-F`/`-c`/`-C`/`-t` bei Token-Laenge >
2) — ohne diesen Check wuerde ein am Stueck gequotetes `-m"..."` (ein
einziger shlex-Token) jedes 'a' in der Nachricht als Commit-Cluster-Treffer
missverstehen.

Nicht mode-aware (bewusst): immer `deny`, kein `ask`-Zweig.

Praefixe: `VAR=value`-Zuweisungspraefixe (`FOO=1 git add -A`) und
Shell-Kopf-Keywords (`! git add -A`, `if git add -A; then`) muessen
abgestreift werden, sonst ist `tokens[0]` nicht `git` und der Aufruf bliebe
unerkannt (ein unanchored Ganzstring-`grep -E` wuerde solche Faelle treffen,
ein Tokenizer ohne Praefix-Behandlung nicht). `_strip_prefixes` deckt sie ab.

Bekannte Luecken (Layer, kein Zaun, strukturell nicht schliessbar — gleiches
Restrisiko wie bei den Geschwister-Gates):
- `git add -u`/`--update` (staged Modifikationen aller getrackten Dateien,
  funktional aequivalent zu `-A` fuer bereits getrackte Aenderungen) sowie
  `git add ./` und `git add :/` — nicht erkannt.
- `--` (End-of-Options) wird nicht respektiert: ein Datei-Name, der zufaellig
  wie ein Flag aussieht (`git add -- -A`), wuerde faelschlich geblockt
  (Over-Block, kein Schutzverlust).
- Quote-interne Kommando-Substitution (`git commit -m "... $(git add -A)..."`)
  wird von shlex als Text behandelt, nicht als von bash auszufuehrender Code
  — strukturell blind, wie bei allen Geschwister-Gates.
- `bash -c "..."`-Indirektion, Shell-Aliase, `git config alias.x 'add -A'`.

Grenze: Blacklist ueber Kommandotext — ein Layer, kein Zaun, strukturell nicht
schliessbar (gleiches Restrisiko wie bei den Geschwister-Gates).
"""
import json
import os
import re
import shlex
import subprocess
import sys

# `&` und `|` sind NICHT immer Trenner — `2>&1`, `&>f`, `>|f`, `>>|f`
# enthalten sie als Teil des Umlenkungs-OPERATORS. `&&`/`||` matchen als eigene
# Alternative vorher. Beide Lookarounds sind NUR zusammen mit _mask_quoted
# korrekt: sie pruefen sonst ein gequotetes/escaptes Zeichen (gemessener Bypass).
SPLIT_RE = re.compile(r"\|\||&&|[;\n]|(?<!>)\||(?<![<>])&(?!>)")
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Platzhalter fuer maskierte Zeichen — laengentreu, damit Match-Offsets aus der
# Maske auf dem Originaltext gueltig bleiben.
_MASK = "x"
# Vollstaendiger Satz Umlenkungs-OPERATOREN (bash+zsh), laengste zuerst. Ohne
# ihn liefe `2>/dev/null rm -rf <ziel>` mit Kopf `2>/dev/null` durch. Jede Form
# an zsh 5.9 gemessen: sie
# alle fuehren das FOLGEwort als Kommando aus. `{fd}>f` fehlt bewusst (weder
# zsh 5.9 noch bash 3.2.57 fuehren dort das Folgewort aus, gemessen).
_REDIR_OP = (r"\d*"
             r"(?:&>>|&>|>>!|>>\||>>|>\||>!|>&|>|<<<|<<-|<<|<>|<&|<)")
# Operator OHNE Ziel (`2> /dev/null`): frisst zusaetzlich das Folgetoken.
REDIRECT_BARE_RE = re.compile(_REDIR_OP + r"\Z")
# Operator mit ANGEHAENGTEM Ziel (`2>/dev/null`): frisst nur sich selbst.
REDIRECT_ATTACHED_RE = re.compile(_REDIR_OP + r"\S+\Z")
# Kommando-Wrapper: sie fuehren das Folgewort als Kommando aus. Enthalten sind
# auch die zsh-Precommand-Modifier `noglob`/`nocorrect`/`exec` und die
# Ausfuehrungs-Wrapper `timeout`/`nice`/`stdbuf`/`setsid`, jeder einzeln an
# zsh 5.9 gemessen.
# Die Shell-Kontrollfluss-Woerter bleiben hier — anders als in merge-gate.py,
# wo sie False Positives gegen harmlosen Code erzeugen wuerden (siehe dort
# _strip_control_flow). Hier haengen Testfaelle daran
# (`if git add -A; then …`), und die Verben dieser Gates kollidieren nicht mit
# gewoehnlichem Fliesstext.
WRAPPER_WORDS = {"sudo", "xargs", "time", "env", "nohup", "command",
                 "noglob", "nocorrect", "exec", "timeout", "nice", "stdbuf",
                 "setsid", "then", "do", "if", "elif", "while", "until", "!"}
# Wrapper mit EIGENEN Optionen — sonst bliebe deren Wert als vermeintliches
# Kommando stehen (`timeout 5 rm -rf <ziel>` haette den Kopf `5`).
WRAPPER_VALUE_FLAGS = {
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "nice": {"-n", "--adjustment"},
    "stdbuf": {"-i", "--input", "-o", "--output", "-e", "--error"},
    "setsid": set(),
}
# Nur eine DAUER darf als timeout-Argument gefressen werden; ein Kommandoname
# passt hier nie hinein, Ueberfressen ist damit ausgeschlossen.
_DURATION_RE = re.compile(r"\A\d+(?:\.\d+)?[smhd]?\Z")
GIT_VALUE_OPTS = ("-C", "-c", "--git-dir", "--work-tree", "--namespace")
COMMIT_ATTACHED_VALUE_PREFIXES = ("-m", "-F", "-c", "-C", "-t")
COMMIT_VALUE_FLAGS = (
    "-m", "--message",
    "-F", "--file",
    "-c", "--reedit-message",
    "-C", "--reuse-message",
    "-t", "--template",
    "--author", "--date", "--cleanup", "--fixup", "--squash", "--trailer",
)

REASON = ("[Git-Add-Commit-Gate] git add ./-A/--all/* und git commit -a "
          "verboten — nur spezifische Dateien stagen: git add <file>")


def _mask_quoted(command):
    """Ersetzt gequotete/escapte Zeichen laengentreu durch _MASK.

    SPLIT_RE darf ausschliesslich ECHTE Shell-Operatoren sehen. Die Lookarounds
    pruefen sonst das ROHE Nachbarzeichen — und in `echo a\\>& rm -rf <ziel>`
    steht vor dem `&` zwar ein `>`, aber ein escaptes: zsh nimmt das `&` als
    Hintergrund-Operator und fuehrt `rm` AUS (mit Sentinel-Skript
    gemessen). Ohne Maske verschmelzen beide Teile zu EINEM Segment mit Kopf
    `echo` — der Bypass, den die Maskierung schliesst. Bei unbalancierten
    Quotes wird das Original benutzt (mehr Splits = mehr geprueft =
    konservativ).
    """
    out = []
    quote = None
    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        if quote is None:
            if c == "\\" and i + 1 < n:
                out.append(_MASK)
                out.append(_MASK)
                i += 2
                continue
            if c in ("'", '"'):
                quote = c
                out.append(_MASK)
                i += 1
                continue
            out.append(c)
            i += 1
            continue
        if quote == '"' and c == "\\" and i + 1 < n:
            out.append(_MASK)
            out.append(_MASK)
            i += 2
            continue
        if c == quote:
            quote = None
        out.append(_MASK)
        i += 1
    if quote is not None:
        return command
    return "".join(out)


def _segments(command):
    # Getrennt wird auf der MASKE, geschnitten aus dem ORIGINAL.
    masked = _mask_quoted(command)
    parts = []
    last = 0
    for m in SPLIT_RE.finditer(masked):
        parts.append(command[last:m.start()])
        last = m.end()
    parts.append(command[last:])
    return [p.strip() for p in parts if p.strip()]


def _strip_wrappers(segment):
    previous = None
    while previous != segment:
        previous = segment
        segment = segment.lstrip("({ \t").rstrip(")} \t")
        if segment.startswith("$("):
            segment = segment[2:]
    return segment


def _eat_wrapper_args(tokens, word):
    # Optionen des Wrappers selbst, damit deren Wert nicht als Kommando gilt.
    flags = WRAPPER_VALUE_FLAGS.get(word)
    if flags is None:
        return
    while tokens and tokens[0].startswith("-") and tokens[0] != "-":
        flag = tokens.pop(0)
        if flag in flags and tokens:
            tokens.pop(0)
    if word == "timeout" and tokens and _DURATION_RE.match(tokens[0]):
        tokens.pop(0)


def _strip_prefixes(tokens):
    """Frisst Umlenkungen, NAME=WERT-Zuweisungen und Wrapper-Woerter am Kopf.

    Arbeitet auf TOKENS statt auf dem Rohstring: ein Regex auf dem Rohstring
    kennt keine Umlenkung — `2>/dev/null <kommando>` behielte das
    Umlenkungs-Token als Kommandokopf und liefe an jeder Verb-Pruefung
    vorbei. Mutiert `tokens` in place.
    """
    while tokens:
        head = tokens[0]
        if ASSIGN_RE.match(head):
            tokens.pop(0)
            continue
        if REDIRECT_BARE_RE.match(head):
            # `2> /dev/null`: das Ziel steht im naechsten Token und gehoert zur
            # Umlenkung. Die BARE-Pruefung MUSS vor der ATTACHED-Pruefung
            # stehen, sonst liest letztere `>>` als Operator `>` mit Ziel `>`
            # und laesst das echte Ziel als Kommando stehen.
            tokens.pop(0)
            if tokens:
                tokens.pop(0)
            continue
        if REDIRECT_ATTACHED_RE.match(head):
            tokens.pop(0)
            continue
        word = os.path.basename(head).lower()
        if word in WRAPPER_WORDS:
            tokens.pop(0)
            _eat_wrapper_args(tokens, word)
            continue
        break
    return tokens


def _tokens(segment):
    # KEIN comments=True. shlex schneidet damit an JEDEM `#` ab, auch
    # mitten im Wort — `rm -rf a#b <echtes-ziel>` verlor sein echtes Ziel.
    # Keine Shell tut das: ein Kommentar beginnt nur am WORTANFANG (an
    # zsh 5.9 per argv-Vergleich gemessen).
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _git_subcommand(tokens):
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t in GIT_VALUE_OPTS:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        break
    if i >= len(tokens):
        return None, []
    return tokens[i], tokens[i + 1:]


def _add_triggers(rest):
    for t in rest:
        if t in ("-A", "--all", ".", "*"):
            return True
        if t.startswith("-") and not t.startswith("--") and len(t) > 1 and "A" in t[1:]:
            return True
    return False


def _commit_triggers(rest):
    i = 0
    while i < len(rest):
        t = rest[i]
        if t in COMMIT_VALUE_FLAGS:
            i += 2
            continue
        if len(t) > 2 and t[:2] in COMMIT_ATTACHED_VALUE_PREFIXES:
            i += 1
            continue
        if t in ("-a", "--all"):
            return True
        if t.startswith("-") and not t.startswith("--") and len(t) > 1 and "a" in t[1:]:
            return True
        i += 1
    return False


def evaluate(command):
    for segment in _segments(command):
        tokens = _strip_prefixes(_tokens(_strip_wrappers(segment)))
        if not tokens or os.path.basename(tokens[0]).lower() != "git":
            continue
        sub, rest = _git_subcommand(tokens)
        if sub == "add" and _add_triggers(rest):
            return True
        if sub == "commit" and _commit_triggers(rest):
            return True
    return False


CASES = [
    # (command, expected)
    ("git add -A", True),
    ("git add --all", True),
    ("git add .", True),
    ("git add *", True),
    ("git -C /Users/alice/project-x add -A", True),
    ("git add -Av", True),
    ("git commit -a", True),
    ("git commit --all", True),
    ("git commit -am 'wip'", True),
    # -ma 'wip' ist KEIN Cluster: git parst "a" als -m-Wert, "wip" als
    # Pathspec (verifiziert gegen echtes git — "pathspec 'wip' did not
    # match"). Keine Commit-All-Semantik, korrekt False.
    ("git commit -ma 'wip'", False),
    ("git add file.py && git commit -a -m 'wip'", True),
    ("git add file.py", False),
    ("git add foo.py bar.py", False),
    ("git commit -m 'fix: something'", False),
    ("git commit -m 'fix: something' --author='A <a@b.c>'", False),
    ("git status", False),
    ("git add file.py\ngit commit -m 'fix: something\n\n* etwas wurde geaendert\n* noch was'", False),
    ("git commit -m 'docs: document the -parallel and -fast flags for -data pipeline'", False),
    ("git add gate-file.py\ngit commit -m 'feat(gate): add gate-level check for parallel data fast path'", False),
    ("git commit -m '- bullet one\n- bullet two'", False),
    ("git commit -F commit-msg.txt", False),
    ("git commit -m -fast", False),
    ("git commit -mAlpha", False),
    ('git commit -m"fix: parallel refactor"', False),
    ("git commit -m'wip: add stuff'", False),
    # Env-Var-Zuweisungspraefix + Shell-Kopf-Keywords: stiller Bypass, wenn
    # sie nicht abgestreift werden.
    ("FOO=1 git add -A", True),
    ("GIT_AUTHOR_NAME=x git commit -a", True),
    ("! git add -A", True),
    ("if git add -A; then echo ok; fi", True),
    # --- Case-Insensitivity (macOS/APFS) ---
    ("GIT add -A", True),
    ("Git add -A", True),
    ("GIT add --all", True),
    ("GIT commit -a", True),
    ("GIT commit -am 'wip'", True),
    ("/opt/homebrew/bin/GIT add -A", True),
    ("SUDO GIT add -A", True),
    # VAR=wert-Praefix kombiniert mit Case-Variation; zusaetzlich der
    # GEQUOTETE Wert (FOO="a b" / FOO='a b').
    ("FOO=1 GIT add -A", True),
    ('FOO="a b" git add -A', True),
    ('FOO="a b" GIT add -A', True),
    ("GIT_AUTHOR_NAME='A B' GIT commit -a", True),
    ("! GIT add -A", True),
    # Kein Over-Block durch die Normalisierung.
    ("GIT add file.py", False),
    ("GIT commit -m 'fix: something'", False),
    ("GIT status", False),
    ("echo 'GIT add -A'", False),
    # ---- Praefixe, die den Kommandokopf verdecken ---------------------------
    ("2>/dev/null git add -A", True),
    ("2> /dev/null git add -A", True),
    ("2>&1 git add -A", True),
    ("&>out git add -A", True),
    ("> out git add -A", True),
    ("<>fileX git add -A", True),
    ("<<<hi git add -A", True),
    (">| out git add -A", True),
    (">|out git add -A", True),
    ("noglob git add -A", True),
    ("nocorrect git add -A", True),
    ("exec git add -A", True),
    ("setsid git commit -a", True),
    ("timeout 5 git add -A", True),
    ("timeout -k 1 5 git add -A", True),
    ("nice -n 5 git add -A", True),
    ("stdbuf -o0 git add -A", True),
    ("2>/dev/null noglob git add -A", True),
    ("echo a\\>& git add -A", True),
    ('echo "a>"& git add -A', True),
    # Gegenprobe: `>&` IST hier eine Umlenkung, `git` ist ein DATEINAME.
    ("echo hi >& git add -A", False),
    # Maskierung fixt nebenbei den Newline-in-Quotes-Fall: eine Commit-Message
    # mit Zeilenumbruch wird nicht mehr mitten durchgeschnitten.
    ("git commit -m 'zeile eins\ngit add -A ist gefaehrlich'", False),
    ("echo '# git add -A'", False),
]

KNOWN_GAPS = [
    "git add -u",
    "git add --update",
    "git add ./",
    "git add :/",
    "git add -- -A",
    'git commit -m "chore $(git add -A)"',
]


def selftest():
    failures = 0
    for command, expected in CASES:
        hit = evaluate(command)
        ok = hit == expected
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'} [{'block' if expected else 'pass '}] {command!r}")
    print(f"\n{len(CASES) - failures}/{len(CASES)} OK")
    print("Bekannte Luecken (Layer, kein Zaun — bewusst nicht abgedeckt):")
    for gap in KNOWN_GAPS:
        print(f"  GAP  {gap!r}  -> {'erkannt' if evaluate(gap) else 'nicht erkannt'}")
    shape_failures = selftest_json_shape()
    sys.exit(1 if failures or shape_failures else 0)



UNPARSEABLE_REASON = (
    "[Git-Add-Commit-Gate] Der Hook hat kein auswertbares `tool_input.command` bekommen "
    "(fehlender oder nicht-stringartiger Wert). Ein nicht interpretierbares "
    "Kommando gilt als UNBEKANNT und wird geblockt statt durchgelassen — ohne "
    "diese Pruefung wuerde der Hook hier mit einer Exception sterben, und ein "
    "abgestuerzter PreToolUse-Hook laesst das Tool laufen (fail-open).")


def _command_of(data):
    """Kommandotext, "" wenn nichts zu pruefen ist, None = unauswertbar."""
    if not isinstance(data, dict):
        return None
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if command is None or command == "":
        return ""
    if not isinstance(command, str):
        return None
    return command


def _deny_unparseable():
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": UNPARSEABLE_REASON}}))

# Entscheidungs-Protokoll (metrics/gates). Fail-open und bewusst defensiv
# importiert: ein fehlender oder kaputter Logger darf dieses Gate nie zum
# Absturz bringen -- ein abgestuerzter PreToolUse-Hook sperrt im schlimmsten
# Fall jeden Bash-Aufruf aus.
GATE_LOG_NAME = "git-add-commit-gate"
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
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    command = _command_of(data)
    if command is None:
        log_decision(GATE_LOG_NAME, "deny", "unparseable-command", data)
        _deny_unparseable()
        return
    if not evaluate(command):
        log_decision(GATE_LOG_NAME, "allow", "no-hit", data)
        sys.exit(0)
    log_decision(GATE_LOG_NAME, "deny", "stage-everything", data)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": REASON}}))


# ---- JSON-Shape-Selftest -------------------------------------
# Die CASES oben pruefen ausschliesslich evaluate() — also die Kommandotext-
# Logik. Der Fail-closed-Guard (`_command_of` + `_deny_unparseable` in main())
# war damit NICHT abgedeckt: er sitzt vor evaluate() und haengt an der FORM des
# stdin-JSON, nicht am Kommandotext. Diese Gruppe ruft deshalb main() echt
# ueber `subprocess` auf — derselbe Interpreter, dieselbe Datei, praeparierter
# stdin — und wertet stdout/Exit-Code aus. Nur so ist verifiziert, dass ein
# kaputt geformtes tool_input tatsaechlich zu `deny` fuehrt und nicht zu einer
# Exception (ein abgestuerzter PreToolUse-Hook laesst das Tool laufen).
#
# Erwartungswerte:
#   "deny-unparseable" — deny mit UNPARSEABLE_REASON (JSON-Shape-Guard)
#   "deny-rule"        — deny mit REASON (echter Treffer der Detection-Logik)
#   "allow"            — kein Output, Exit 0
JSON_SHAPE_CASES = [
    # (Label, stdin-Text, Erwartung)
    ("Top-Level JSON-Array statt Objekt", "[]", "deny-unparseable"),
    ("Top-Level JSON-String statt Objekt", '"x"', "deny-unparseable"),
    ("tool_input ist nicht-leere Liste", '{"tool_input": ["x"]}',
     "deny-unparseable"),
    ("command ist int", '{"tool_input": {"command": 123}}', "deny-unparseable"),
    ("command ist Liste",
     '{"tool_input": {"command": ["git", "add", "-A"]}}', "deny-unparseable"),
    # Regressionswaechter: der legitime "nichts zu pruefen"-Pfad (command == "")
    # darf durch den Guard NICHT zu deny werden.
    ("leeres Objekt, kein tool_input", "{}", "allow"),
    ("tool_input ist null", '{"tool_input": null}', "allow"),
    # Ist-Verhalten, gemessen: `data.get("tool_input") or
    # {}` macht aus JEDEM falsy Wert ein leeres dict — eine LEERE Liste landet
    # deshalb im "nichts zu pruefen"-Pfad statt beim Non-dict-Guard. Kein
    # Bypass (in einem falsy Wert steckt kein Kommando), aber die Form-Pruefung
    # greift hier nicht. Festgehalten wie es IST.
    ("tool_input ist leere Liste (falsy -> {})", '{"tool_input": []}', "allow"),
    ("harmloser String-Command", '{"tool_input": {"command": "git status"}}', "allow"),
    # Gegenprobe: der Guard verschluckt echte Treffer nicht.
    ("echter Treffer aus CASES", '{"tool_input": {"command": "git add -A"}}', "deny-rule"),
    # Kaputtes JSON: bewusst dokumentiertes Verhalten in main()
    # (`except Exception: sys.exit(0)`). Hier nur festgehalten.
    ("kein gueltiges JSON", "{not json", "allow"),
]


def _run_hook(stdin_text):
    return subprocess.run(
        [sys.executable, os.path.abspath(__file__)],
        input=stdin_text, capture_output=True, text=True, timeout=30)


def _hook_outcome(proc):
    if proc.returncode != 0 and not proc.stdout.strip():
        return f"crash(rc={proc.returncode})"
    if not proc.stdout.strip():
        return "allow"
    try:
        payload = json.loads(proc.stdout)
    except Exception:
        return "kein-json"
    hook_output = payload.get("hookSpecificOutput") or {}
    if hook_output.get("permissionDecision") != "deny":
        return "kein-deny"
    reason = hook_output.get("permissionDecisionReason", "")
    if reason == UNPARSEABLE_REASON:
        return "deny-unparseable"
    if reason == REASON:
        return "deny-rule"
    return "deny-fremder-grund"


def selftest_json_shape():
    print("\nJSON-Shape-Guard (main() end-to-end ueber subprocess):")
    failures = 0
    for label, stdin_text, expected in JSON_SHAPE_CASES:
        actual = _hook_outcome(_run_hook(stdin_text))
        ok = actual == expected
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'} [{expected}] {label} "
              f"-> {actual}  stdin={stdin_text!r}")
    print(f"\n{len(JSON_SHAPE_CASES) - failures}/{len(JSON_SHAPE_CASES)} OK "
          "(JSON-Shape)")
    return failures


if __name__ == "__main__":
    main()
