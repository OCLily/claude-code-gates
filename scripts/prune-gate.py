#!/usr/bin/env python3
# Summary (English; the German docstring below is the long-form design record):
# PreToolUse hook for the Bash tool that stops an agent from deleting its own
# skills, agents, plans and notes. It matches `rm` and `git rm` on TOKENS (not raw
# text), resolves relative targets against the cwd of the hook payload and compares
# paths component-wise and case-folded against GUARDED_ROOTS (~/.claude/agents,
# skills, plans, scripts, ~/vault/memory, projects, ~/project-x/knowledge). Brace
# and glob forms count, and so do deletion APIs with a guarded path inside inline
# interpreter code or a heredoc (matched with regexes on the segment text, not on
# tokens). `git mv` is deliberately allowed. Mode-aware:
# "ask" in default/plan mode, "deny" in every auto-approve mode. Unparseable stdin
# fails open; valid JSON of the wrong shape is denied. A layer, not a sandbox:
# the known holes are listed in KNOWN_GAPS.
"""prune-gate.py — PreToolUse Bash-Gate gegen autonomes Löschen von Skills/Agents.

Die Regel "nie autonom Skills/Agents löschen, immer fragen" ist als reiner
Appell nicht durchsetzbar. Im Auto-Approve-Modus ist weder eine
permissions.ask-Regel noch ein Hook-`ask` garantiert — nur `deny` ist dort
durchsetzbar. Deshalb mode-aware, nach demselben Muster wie die übrigen Gates
dieser Schicht:
- default / plan                                     -> ask  (Prompt an den Maintainer)
- acceptEdits / bypassPermissions / unbekannter Mode -> deny (fail-closed)

Abdeckung: `rm` und `git rm` (auch hinter `VAR=wert`-Zuweisungen und
sudo/xargs/env/time — beides case-insensitiv, auch in ;/&&/||/|-Ketten) mit
einem Ziel-Pfad, der real (nicht nur textuell) unter einer der geschützten
Wurzeln liegt:
- `~/.claude/agents`, `~/.claude/skills`
- `~/.claude/plans`
- `~/.claude/scripts`
- `~/vault/memory`, `~/vault/projects`
- `~/project-x/knowledge`
Auch Pläne, Notizen und Vault-Wissen sollen nicht autonom gelöscht werden, nicht
nur Skills/Agents. Weil die Wurzeln auf drei Basisverzeichnissen liegen
(CLAUDE_DIR, VAULT_DIR, PROJECT_X_DIR), ist GUARDED_ROOTS eine flache Liste
absoluter realpath-Konstanten statt einer CLAUDE_DIR-relativen Ableitung.
Geschützt sind ausschließlich die genannten Verzeichnisse — Geschwister wie
`~/vault/ideas` sind bewusst NICHT gegated.

Kommando-Erkennung läuft über TOKEN, nicht über Segment-TEXT: das
Kommando ist `os.path.basename(tokens[0]).lower()` nach
`_strip_prefixes(_strip_wrappers(...))` — identisch zu destructive-command-gate,
push-gate, merge-gate und git-add-commit-gate. Regexe wie
`^rm(?:\\s|$)` / `^git\\s+…rm` auf dem rohen Segment-Text liessen jeden
Pfad-Praefix durch: `/bin/rm -f ~/.claude/agents/critic.md`,
`/opt/homebrew/bin/rm -rf skills/foo` und `RM -f agents/foo.md` waeren strukturell
kein Treffer. Der destructive-command-gate-Backstop deckt davon nur
force+recursive ab, also gerade NICHT die Einzeldatei-Loeschung. Die
Flag-Kombination ist fuer diesen Gate ohnehin irrelevant: geprueft wird der
Loesch-CHARAKTER des Kommandos plus der Ziel-Pfad, nicht `-r`/`-f` — `rm
agents/x.md` ohne jedes Flag ist derselbe Treffer wie `rm -rf`.
`git rm` wird ebenfalls ueber Tokens geparst (Subkommando nach den
git-Value-Optionen), damit `-r`/`-f`/`--cached` in beliebiger Reihenfolge
greifen und `git -c foo=bar rm …` nicht faelschlich als
Arbeitsverzeichnis-Wechsel gelesen wird — nur `-C` ist ein chdir.

Der Ziel-Pfad-Vergleich läuft komponentenweise, case-gefaltet und glob-bewusst
(`_covers`) statt über String-Prefixe. Ein reiner Prefix-Vergleich hätte drei
Löcher:
- Parent-Richtung: er kennt nur "Ziel unter Wurzel". `rm -rf ~/.claude`, `rm -rf .`
  (cwd = CLAUDE_DIR), `git -C ~/.claude rm -r .` und `rm -rf /` liefen durch,
  obwohl sie agents/skills/plans genauso real vernichten. Deshalb trifft auch die
  Gegenrichtung (Wurzel unter Ziel).
- Glob: der Hook sieht den Kommandotext VOR der Shell-Expansion, `rm -rf
  ~/.claude/*` ist textuell kein Prefix irgendeiner Wurzel. Der komponentenweise
  fnmatch schliesst das, ohne `rm -rf *.log` in ~/.claude mitzublocken.
- Gross-/Kleinschreibung: der Kommando-Kopf wird case-insensitiv verglichen, der
  ZIELPFAD muss es auch sein. Auf dem Default-APFS (case-insensitiv: `ls
  ~/.claude/SKILLS` listet skills/) wäre `rm -rf ~/.claude/SKILLS/foo` sonst ein
  echter Bypass. Beide Seiten werden gefaltet — auf einem case-SENSITIVEN
  Volume wäre das Over-Blocking, die sichere Fehlrichtung.
Zwei weitere Richtungen derselben Familie:
(a) `os.path.normpath` lässt genau zwei führende Slashes stehen, und
`//Users/alice/.claude/skills` ist auf macOS ein lebender Pfad auf dieselbe
Inode — das Fallen leerer Komponenten in `_parts` schliesst das mit.
(b) OFFEN: der Firmlink `/System/Volumes/Data/Users/…` zeigt ebenfalls auf
dieselbe Inode, und `realpath` normalisiert ihn NICHT zurück — ein Fix braucht
Alias-Präfix-Regel oder st_dev/st_ino-Vergleich, nicht Textvergleich
(s. KNOWN_GAPS).

Brace-Expansion ist eine weitere Richtung derselben Familie: `rm -rf
~/.claude/{skills,agents}` vernichtet in bash UND zsh beide Kern-Wurzeln, denn
die Shell expandiert `{a,b}` VOR der Ausführung — der Hook sieht den Text davor,
und `fnmatch` kennt `{}` nicht als Metazeichen (`fnmatchcase("agents",
"{skills,agents}")` ist False). Dieselbe Lücke in Parent-Richtung:
`rm -rf ~/{.claude,vault}`. Gelöst NICHT durch Nachbau der Brace-Expansion
(fehleranfällig und selbst wieder umgehbar), sondern konservativ: enthält eine
Ziel-Komponente `{`, `}` oder `,`, gilt sie als Treffer-Verdacht und matcht jede
Wurzel-Komponente auf ihrer Ebene. Die naheliegende Alternative "Brace-Zeichen
entfernen, dann vergleichen" fängt den KERNFALL nicht: `{skills,agents}` wird
dabei zu `skillsagents` und matcht danach weder `skills` noch `agents`.
Scharf gestellt wird der Verdacht nur, wenn das
KOMMANDO-TOKEN selbst Brace-Zeichen enthält (`brace_suspect`) — ein `cwd` mit
Komma im Verzeichnisnamen darf die Freiheit fremder Repos
nicht wieder aufweichen. Bewusst akzeptierter Over-Block-Preis: `rm -rf
~/.claude/{plugins,todos}`, `rm -rf ~/{Downloads,Documents}` und das gar nicht
brace-expandierende `rm -rf ~/.claude/s*,a*` blocken mit, obwohl sie keine
Wurzel treffen.

Relative Tokens werden gegen den `cwd` aus dem Hook-Payload aufgelöst
(`os.path.join(cwd, token)` + normpath), nicht gegen den Text allein. Grund:
der Hook ist GLOBAL registriert (jede
Session, jedes Projekt) — ein rein textueller Treffer auf "skills/"/"agents/"
hätte `rm -rf skills/foo` auch in einem völlig fremden Repo mit eigenem
skills/-Ordner blockiert. Fehlt `cwd` im Payload (sollte laut
Claude-Code-Hook-Schema nicht vorkommen): relative Tokens werden NICHT
geflaggt — Under-Protection im Rand-Fall ist hier bewusst der sicherere Fehler
als das Over-Blocking. `git mv` ist explizit KEIN Treffer —
Promote/Rename (z.B. skills/learned/* -> skills/*) bleibt frei. Wörter wie
"skills"/"agents" in grep/ls/cat/echo lösen nicht aus; nur ein echtes
Lösch-Kommando mit real aufgelöstem Treffer-Pfad. Mit der Parent-Richtung ist
`rm -rf .` kontextabhängig: in ~/.claude ein Treffer, in
~/.claude/plugins und in jedem fremden Repo nicht.

Grenze: Blacklist über Kommandotext — ein Layer, kein Zaun, strukturell nicht
schließbar (gleiches Restrisiko wie beim git-add-Blocker-Hook). Die bekannten
Restlücken stehen in KNOWN_GAPS und werden im Selftest mit ihrem
Ist-Ergebnis ausgegeben (einzelne Einträge werden erkannt und bleiben als Pin
stehen); Kurzfassung: cwd-Wechsel in der Kette, Variablen-
EXPANSION (`D=skills/foo; rm -rf $D` — das vorangestellte `VAR=wert`-PRÄFIX
selbst ist dagegen abgedeckt, ebenso wie `sudo`/`SUDO`, Umlenkungen am Anfang
und die Wrapper `time`/`timeout`/`nice`/`stdbuf`/`setsid` aus WRAPPER_WORDS;
offen bleibt davon `env -i`, weil danach ein Flag statt eines Kommandos am
Segment-Anfang steht), Aliase, Wrapper-Skripte und Wrapper-Namen ausserhalb von
WRAPPER_WORDS (`caffeinate`, `doas`, `gtimeout`),
Kommando im gequoteten Argument
(`bash -c "…"`) oder in einer eingebetteten Kommandosubstitution (`git commit -m
"$(rm …)"` — ein eigenständiges `$(rm …)` als ganzes Segment wird dagegen
erkannt), Pipe-Ketten mit Pfad im Vorsegment, `find … -delete` und `find …
-exec rm`, andere Lösch-Verben (`unlink`/`trash`/`shred`), Wegbewegen statt Löschen (`mv`),
Inhalts-Vernichtung per Redirect/`truncate`, `--git-dir`/`--work-tree`-
Umlenkung, Symlink-Indirektion, der macOS-Firmlink-Alias
`/System/Volumes/Data/Users/…`, git-Unterkommandos die ohne `rm`-Verb
vernichten (`clean -fd`, `checkout -- <pfad>`, `restore`) und jedes Löschen
ausserhalb des Bash-Tools.
Echte Absicherung bleibt Verhaltensregel + git-History.

Bewusst fail-open bei kaputtem/fehlendem stdin-JSON (except: sys.exit(0)): ein
malformed Payload blockt hier nie einen Tool-Call, dieselbe Abwaegung wie in
allen anderen Gates dieser Schicht. AUSNAHME: JSON, das zwar
parst, aber kein auswertbares `tool_input.command` traegt (Payload kein Objekt,
`tool_input` kein Objekt, `command` weder String noch leer), wird geblockt statt
durchgelassen — sonst stuerbe main() dort an einem AttributeError, und ein
abgestuerzter PreToolUse-Hook laesst das Tool laufen. Das ist kein Fail-open aus
Abwaegung, sondern ein Absturz; `_command_of`/`_deny_unparseable` fangen diesen
Fall ab.

Zweite Richtung derselben Absturz-Familie: der `cwd` aus dem Payload geht
ungeprueft in `os.path.join`. Ein vorhandener, aber nicht-stringartiger Wert
(`"cwd": 42`, `"cwd": [...]`) stuerbe dort mit einem TypeError — in
`_hits_guarded` (relativer Token) bzw. in `_resolve_cdirs` (`git -C <relativ>`).
Kein Deny-Output, also fail-open, und zwar ausgerechnet auf dem Pfad, auf dem ein
`rm -rf skills/foo` sonst hart blockt. `_cwd_of` prueft deshalb
`isinstance(cwd, str)` und blockt bei Fehlschlag mit eigenem Reason-Text —
gleiche Fehlerrichtung wie beim `command`: nicht auswertbar ⇒ deny. FALSY
Nicht-Strings (`null`, `0`, `false`, `[]`) sind gleichbedeutend mit "kein cwd"
(-> ""), weil `data.get` per `or ""` so gelesen wird; relative Tokens sind dann
nicht aufloesbar und werden nicht geflaggt (dokumentierte Under-Protection, s.o.).
Geringes Risiko: `cwd` stammt vom Harness, nicht vom Agenten — es gibt keinen
bekannten Einflusspfad, der den Typ von aussen setzt.

Getestet werden diese Payload-Guards in zwei Gruppen, beide end-to-end ueber
einen echten Subprozess mit echtem stdin (JSON_SHAPE_CASES teilt sich den Runner
mit PAYLOAD_CASES, siehe dort): PAYLOAD_CASES fuer die `cwd`-Typfrage,
JSON_SHAPE_CASES fuer die FORM des Payloads (Top-Level kein Objekt,
`tool_input` kein Objekt, `command` kein String) plus die Reihenfolge der
Guards untereinander und die Mode-Blindheit des Shape-Guards. Beide Gruppen
schreiben ausserdem zwei asymmetrische Ist-Verhalten fest, die NICHT gefixt
sind: ein FALSY nicht-Objekt in `tool_input` (`[]`, `null`, `""`, `0`) faellt
ueber `or {}` auf "nichts zu pruefen" zurueck und wird durchgelassen, waehrend
ein FALSY nicht-String in `command` (`false`, `0`, `[]`) geblockt wird — beim
`cwd` ist es umgekehrt. Kein aktives Loch (ohne auswertbares `command` gibt es
nichts auszufuehren), aber die Guard-Zusage "nicht auswertbar ⇒ deny" gilt
eben nicht flaechendeckend; ein spaeterer Fix muss das bewusst umdrehen.
"""
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys

CLAUDE_DIR = os.path.realpath(os.path.expanduser("~/.claude"))
VAULT_DIR = os.path.realpath(os.path.expanduser("~/vault"))
PROJECT_X_DIR = os.path.realpath(os.path.expanduser("~/project-x"))
GUARDED_ROOTS = [
    os.path.join(CLAUDE_DIR, "agents"),
    os.path.join(CLAUDE_DIR, "skills"),
    os.path.join(CLAUDE_DIR, "plans"),
    # scripts/ gehoert dazu: alle Gates dieses Harness liegen dort, ein
    # Loeschen von scripts/ nimmt jede Detektion auf einmal weg. Ein
    # Schreibschutz fuer scripts/*.py gegen Write|Edit (anderer Hook) deckt Bash
    # nicht ab.
    os.path.join(CLAUDE_DIR, "scripts"),
    os.path.join(VAULT_DIR, "memory"),
    os.path.join(VAULT_DIR, "projects"),
    os.path.join(PROJECT_X_DIR, "knowledge"),
]


def _parts(path):
    """Pfad -> case-gefaltete, leer-bereinigte Komponentenliste.

    `.lower()` weil das Ziel auf dem case-INSENSITIVEN Default-APFS liegt
    (`ls ~/.claude/SKILLS` listet den Inhalt von skills/) — ohne Faltung
    waere `rm -rf ~/.claude/SKILLS/foo` ein echter Bypass. Das Fallen
    leerer Komponenten normalisiert zusaetzlich `//`: `os.path.normpath` LAESST
    genau zwei fuehrende Slashes stehen (POSIX-implementation-defined), und
    `//Users/alice/.claude/skills` ist auf macOS ein lebender Pfad auf dieselbe
    Inode — ohne das Fallen ebenfalls ein Bypass.
    """
    return [p for p in path.lower().split("/") if p]


GUARDED_ROOT_PARTS = [_parts(r) for r in GUARDED_ROOTS]
BRACE_CHARS = "{},"


def _brace_ish(text):
    """True wenn `text` Brace-Expansions-Syntax enthalten KANN.

    Bewusst keine Simulation der Expansion, nur ein Verdachtsflag: die drei
    Zeichen `{`, `}` und `,` sind zusammen die gesamte Oberflaeche von
    `{a,b}` und `{a..z}`. Das Komma allein expandiert zwar nie (im Glob ist es
    ein normales Zeichen), bleibt aber drin — es kostet nur Over-Block in der
    sicheren Richtung und deckt halb-gequotete Reste mit ab.
    """
    return any(c in text for c in BRACE_CHARS)


# `&` und `|` sind NICHT immer Trenner — `2>&1`, `&>f`, `>|f`, `>>|f`
# enthalten sie als Teil des Umlenkungs-OPERATORS. `&&`/`||` matchen als eigene
# Alternative vorher. Beide Lookarounds sind NUR zusammen mit _mask_quoted
# korrekt: sie pruefen sonst ein gequotetes/escaptes Zeichen (gemessener Bypass).
SPLIT_RE = re.compile(r"\|\||&&|[;\n]|(?<!>)\||(?<![<>])&(?!>)")
# Eine vorangestellte `VAR=wert`-Zuweisung verdeckt sonst den Kommando-Kopf
# (`GIT_TERMINAL_PROMPT=0 rm -rf skills/foo` liefe als Nicht-Treffer durch);
# gequotete Werte mit Leerzeichen (`D="a b" rm …`) zerlegt shlex in ein Token.
# Die Wrapper-Woerter werden ueber `.lower()` verglichen: auf case-insensitivem
# APFS loest /usr/bin/SUDO real auf /usr/bin/sudo auf, `SUDO RM -rf skills/foo`
# ist also ein echter Aufruf.
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Platzhalter fuer maskierte Zeichen — laengentreu, damit Match-Offsets aus der
# Maske auf dem Originaltext gueltig bleiben.
_MASK = "x"
# Vollstaendiger Satz Umlenkungs-OPERATOREN (bash+zsh), laengste zuerst. Ohne
# sie liefe `2>/dev/null rm -rf <ziel>` mit Kopf `2>/dev/null` durch. Jede Form
# an zsh 5.9 gemessen: sie alle fuehren das FOLGEwort als Kommando aus.
# `{fd}>f` fehlt bewusst (weder zsh 5.9 noch bash 3.2.57 fuehren dort das
# Folgewort aus, gemessen).
_REDIR_OP = (r"\d*"
             r"(?:&>>|&>|>>!|>>\||>>|>\||>!|>&|>|<<<|<<-|<<|<>|<&|<)")
# Operator OHNE Ziel (`2> /dev/null`): frisst zusaetzlich das Folgetoken.
REDIRECT_BARE_RE = re.compile(_REDIR_OP + r"\Z")
# Operator mit ANGEHAENGTEM Ziel (`2>/dev/null`): frisst nur sich selbst.
REDIRECT_ATTACHED_RE = re.compile(_REDIR_OP + r"\S+\Z")
# Kommando-Wrapper: sie fuehren das Folgewort als Kommando aus. Dazu zaehlen die
# zsh-Precommand-Modifier `noglob`/`nocorrect`/`exec` und die Ausfuehrungs-
# Wrapper `timeout`/`nice`/`stdbuf`/`setsid`, jeder einzeln an zsh 5.9 gemessen.
# Die Shell-Kontrollfluss-Woerter stehen hier mit drin — anders als in
# merge-gate.py, wo sie einen reproduzierten False Positive erzeugten. Hier
# haengen Testfaelle daran (`if git add -A; then …`), und die Verben dieser
# Gates kollidieren nicht mit gewoehnlichem Fliesstext.
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


def _mask_quoted(command):
    """Ersetzt gequotete/escapte Zeichen laengentreu durch _MASK.

    SPLIT_RE darf ausschliesslich ECHTE Shell-Operatoren sehen. Die Lookarounds
    pruefen sonst das ROHE Nachbarzeichen — und in `echo a\\>& rm -rf <ziel>`
    steht vor dem `&` zwar ein `>`, aber ein escaptes: zsh nimmt das `&` als
    Hintergrund-Operator und fuehrt `rm` AUS (mit Sentinel-Skript
    gemessen). Ohne Maske verschmelzen beide Teile zu EINEM Segment mit Kopf
    `echo` — genau der Bypass, an dem eine Loesung nur mit Lookarounds
    scheitert. Bei unbalancierten Quotes wird das Original benutzt (mehr
    Splits = mehr geprueft = konservativ).
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

    Arbeitet auf TOKENS statt auf dem Rohstring: ein Regex-Praefix kennt keine
    Umlenkung — `2>/dev/null <kommando>` behielte das Umlenkungs-Token als
    Kommandokopf und liefe an jeder Verb-Pruefung vorbei. Mutiert `tokens` in
    place.
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
    # zsh 5.9 per argv-Vergleich gemessen, 21/21 Faelle identisch).
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _git_rm_args(tokens):
    # git -C <dir> setzt das Arbeitsverzeichnis für DIESEN Befehl explizit —
    # überschreibt den Payload-cwd, sonst wuerde z.B. "git -C ~/.claude rm
    # agents/foo.md" aus einem anderen ambient-cwd faelschlich nicht erkannt.
    # `-c key=val` ist
    # dagegen Config, kein chdir — es wird nur uebersprungen.
    cdirs = []
    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token in GIT_VALUE_OPTS:
            if token == "-C" and i + 1 < len(tokens):
                cdirs.append(tokens[i + 1])
            i += 2
            continue
        if token.startswith("-C") and len(token) > 2:
            cdirs.append(token[2:])
            i += 1
            continue
        if token.startswith("-"):
            i += 1
            continue
        break
    if i >= len(tokens) or tokens[i] != "rm":
        return None
    return cdirs, tokens[i + 1:]


def _resolve_cdirs(cdirs, cwd):
    result = cwd
    for cdir in cdirs:
        d = os.path.expanduser(os.path.expandvars(cdir.strip("'\"")))
        result = d if os.path.isabs(d) else (os.path.join(result, d) if result else "")
    return result


def _delete_args(segment, cwd):
    """(zu pruefende Argument-Tokens, effektiver cwd) oder None wenn kein Loeschbefehl."""
    tokens = _strip_prefixes(_tokens(_strip_wrappers(segment)))
    if not tokens:
        return None
    head = os.path.basename(tokens[0]).lower()
    if head == "rm":
        return tokens[1:], cwd
    if head != "git":
        return None
    parsed = _git_rm_args(tokens)
    if parsed is None:
        return None
    cdirs, args = parsed
    return args, _resolve_cdirs(cdirs, cwd)


def _covers(target, root, brace_suspect=False):
    """True wenn Ziel und Wurzel auf derselben Kette liegen — in BEIDE Richtungen.

    Verglichen wird komponentenweise ueber die kuerzere der beiden Listen. Damit
    ist ein Treffer:
    - Ziel == Wurzel                     (`rm -rf ~/.claude/skills`)
    - Ziel UNTER der Wurzel              (`rm -rf ~/.claude/skills/foo`)
    - Ziel UEBER der Wurzel              (`rm -rf ~/.claude`, `rm -rf .`, `rm -rf /`)
    Die dritte Richtung ist die Parent-Richtung: ein reiner Praefix-Vergleich
    (`norm == r or norm.startswith(r + "/")`) laesst jede Parent-Loeschung
    durch, obwohl die die Wurzeln genauso real vernichtet.

    `fnmatch` statt `==` pro Komponente, weil der Hook den Kommandotext VOR der
    Shell-Expansion sieht: `rm -rf ~/.claude/*` ist textuell kein Prefix der
    Wurzeln und lief deshalb durch. Komponentenweise (nicht auf dem ganzen Pfad)
    gematcht, damit ein Glob nur die Ebene trifft, auf der er steht — `rm -rf
    *.log` in ~/.claude bleibt erlaubt (`fnmatch("skills", "*.log")` ist False),
    waehrend `rm -rf ~/.claude/s*` blockt.

    Leere Ziel-Liste (`rm -rf /`) trifft ueber `all([])` jede Wurzel — gewollt.

    `brace_suspect` deckt Brace-Expansion ab: die Shell expandiert `{a,b}` VOR dem Hook,
    `fnmatch` kennt `{}` aber nicht als Metazeichen — `{skills,agents}` wurde
    also literal verglichen und lief durch. Statt die Expansion nachzubauen
    (fehleranfaellig) gilt jede Komponente mit `{`/`}`/`,` als Treffer auf ihrer
    Ebene. Das Flag kommt vom KOMMANDO-Token, nicht vom cwd: sonst wuerde ein
    fremdes Repo namens `some,other-repo` mit eigenem skills/ wieder faelschlich
    blocken (genau das Over-Blocking, das bereits abgeraeumt wurde).
    """
    return all(
        (brace_suspect and _brace_ish(target[i])) or fnmatch.fnmatchcase(root[i], target[i])
        for i in range(min(len(target), len(root)))
    )


def _hits_guarded(token, cwd, *, braces=True, bare_root=True):
    candidate = token.strip().strip("'\"")
    if not candidate or candidate.startswith("-"):
        return False
    expanded = os.path.expanduser(os.path.expandvars(candidate))
    if not os.path.isabs(expanded):
        if not cwd:
            # cwd unbekannt -> relativer Token nicht real aufloesbar, nicht flaggen
            # (Under-Protection im Rand-Fall statt Over-Blocking in fremden Repos).
            return False
        expanded = os.path.join(cwd, expanded)
    target = _parts(os.path.normpath(expanded))
    # Leere Komponentenliste. `_parts("/")` ist `[]`, und
    # `_covers` laeuft darauf ueber `all([])` in True — fuer JEDE Wurzel. Fuer ein
    # `rm`-ZIEL ist das gewollt (`rm -rf /` vernichtet die Wurzeln real), fuer ein
    # beliebiges Literal im Segment nicht: `rmtree("/")` traefe sonst dieses Gate
    # statt das danebenliegende rekursiv-Loesch-Gate, und `"."`/`"~"` erbten
    # ueber die Parent-Richtung dasselbe. Deshalb abschaltbar, nicht entfernt.
    if not target and not bare_root:
        return False
    brace_suspect = braces and _brace_ish(candidate)
    return any(_covers(target, root, brace_suspect) for root in GUARDED_ROOT_PARTS)


# Loeschen ueber eine Interpreter-API statt ueber `rm`. Die
# Schwester-Schicht destructive-command-gate.py faengt das zwar inhaltlich
# (Signatur in der Nutzlast), aber ihre kopfgebundene Erkennung haengt am
# Interpreter-KOPF — und der ist eine OFFENE Menge. Beispiele mit realem Ziel
# unter einer GUARDED_ROOT, die ein kopfgebundener Zweig verfehlt:
#   python3.12 -c / python3.13 -c / python3 -X dev -c / python3 -W ignore -c
#   nodejs -e / node22 -e / bun -e / deno eval / npx tsx -e / uv run python -c
# Deshalb hier die Gegenrichtung: verankert am geschlossenen OBJEKT (die
# GUARDED_ROOTS), kommando-KOPF-blind. Treffer = ein Segment enthaelt eine
# Loesch-API-Signatur UND ein gequotetes String-Literal, das real unter einer
# Wurzel liegt. Weil `_segments` auch an `\n` trennt, deckt das Heredoc-Ruempfe
# mit ab, ohne dass der Heredoc-Kopf erkannt werden muss.
#
# Die Signatur-Menge ist NICHT selbst erfunden (eine autoren-imaginierte
# Verb-Blacklist faengt nur die Faelle des Autors), sondern an die
# Loesch-API-Signaturen von destructive-command-gate.py angelehnt und um
# `.remove(`/`.delete(` erweitert (fs-extra, Deno). Sie bleibt trotzdem eine offene
# Menge — der GESCHLOSSENE Teil dieses Musters ist der Pfad, nicht das Verb.
#
# Warum Pfade aus QUOTED LITERALS statt aus Tokens: das ist die Grenze zwischen
# Loeschen und Lesen/Suchen. `grep -rn "rmtree" ~/.claude/scripts/` traegt den
# Pfad als blankes Token (kein Treffer), eine Interpreter-Nutzlast traegt ihn
# immer gequotet in der Klammer. Verworfene Alternative: Pfad-Treffer aus ALLEN
# Tokens plus Reader-Whitelist (grep/cat/rg/...) — verworfen, weil die Whitelist
# dieselbe offene Menge ist wie die Verb-Liste, nur in Fehlrichtung Over-Block,
# und weil jedes nicht gelistete Lesewerkzeug damit die Harness-Wartung selbst
# blockiert haette (gate-Pflege greppt routinemaessig nach genau diesen
# API-Namen unter scripts/).
#
# Signatur und Pfad muessen GEKOPPELT sein. Ein Treffer, sobald IRGENDWO im
# Segment eine Signatur steht und IRGENDWO im Segment ein Literal unter einer
# Wurzel liegt — ohne Bezug —, waere falsch:
# - `grep -rn 'unlink(' '/Users/alice/.claude/scripts'` -> Block. Das ist
#   GATE-PFLEGE und darf nicht blocken.
# - `grep -rn "shutil.rmtree" "$HOME/.claude/scripts"` -> dito (expandvars).
# - Die QUOTING-FORM wuerde die Entscheidung kippen: derselbe grep mit
#   unquotetem Pfad liefe durch, mit gequotetem blockte er. Dieselbe Handlung,
#   zwei Ergebnisse.
# - `os.remove("/tmp/x"); print(open("/Users/alice/.claude/scripts/x.py").read())`
#   -> Block, obwohl das Loeschziel `/tmp` ist.
# Gescannt wird deshalb nur der ARGUMENTBEREICH des Aufrufs: ab dem
# Signatur-Match bis zur zugehoerigen schliessenden Klammer, begrenzt auf die
# Quote-Spanne, in der die Signatur selbst steht. Ein Literal ausserhalb dieser
# Spanne ist kein Ziel. Bestehen bleibt der Over-Block auf Nutzlast-als-TEXT
# (`echo 'shutil.rmtree("…/skills")'`) — da stehen Verb und Pfad wirklich im
# selben Aufruf, dieselbe Klasse, die destructive-command-gate heute schon hat.
#
# Bewusst offen: eine Loesch-API ausserhalb der Signatur-Menge (neue Library),
# ein per Variable zusammengesetzter Pfad, und Code, der gar nicht im
# Kommandotext steht (Datei-Skript, `exec(b64decode(...))`). Siehe KNOWN_GAPS.
# Drei UNABHAENGIGE Durchlaeufe, nicht eine Alternation: die Nutzlast steckt
# in Shell-Quotes, und ein einziger Durchlauf frisst mit dem AEUSSEREN
# `'...'` die ganze Payload als EIN Literal — der Pfad in den inneren
# `"..."` wird dann nie gesehen.
# Backticks sind der dritte: `` rmSync(`/Users/alice/.claude/skills`) ``
# (Template-Literal = idiomatische JS-Stringform) braucht ihn, und `bun`
# steht nicht in destructive-command-gate.INLINE_CODE_FLAGS — der
# kopfgebundene Zweig der Nachbarschicht greift dort also nicht. Genau die
# Klasse, fuer die dieses Muster gebaut ist.
LITERAL_RES = (
    re.compile(r"'([^']*)'"),
    re.compile(r"\"([^\"]*)\""),
    re.compile(r"`([^`]*)`"),
)
DELETE_API_RE = re.compile(
    r"shutil\.(?:rmtree|move)"
    r"|\brmtree\s*\(|\bremove_tree\s*\("
    r"|os\.(?:remove|unlink|rmdir|removedirs|rename|replace)\s*\("
    r"|\bunlink\s*\(|\brmdir\s*\("
    r"|\brm\s*\(|\brm[Ss]ync\s*\(|\brmdir[Ss]ync\s*\(|\bunlink[Ss]ync\s*\("
    r"|\bpromises\.rm\b"
    r"|Deno\.remove(?:Sync)?\s*\("
    r"|FileUtils\.rm|File\.delete|Dir\.(?:delete|rmdir)"
    r"|\bremove\s*\(|\bdelete\s*\(")


# Laengenschranke dieses Kanals, FAIL-CLOSED -- gleiche Zahl und gleiche
# Begruendung wie in destructive-command-gate.py, s. dort. Greift nur bei
# Segmenten, die zusaetzlich eine Loesch-API-Signatur tragen.
COUPLED_SEGMENT_MAX = 65536
COUPLED_TOO_LONG = "<segment ueber 64 KiB mit Loesch-API-Signatur>"


def _literal_matches(segment):
    return [m for rx in LITERAL_RES for m in rx.finditer(segment)]


def _quote_bounds(segment, pos, *, lits=None):
    """Innen-Grenzen der ENGSTEN Quote-Spanne um `pos`, sonst das Segment."""
    lo, hi = 0, len(segment)
    for m in (_literal_matches(segment) if lits is None else lits):
        if m.start(1) <= pos < m.end(1) and m.end(1) - m.start(1) < hi - lo:
            lo, hi = m.start(1), m.end(1)
    return lo, hi


def _closing_paren(segment, open_at, hi):
    depth = 0
    for j in range(open_at, hi):
        if segment[j] == "(":
            depth += 1
        elif segment[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    return hi


# Empfaenger-Form: `Path("agents/critic.md").unlink()`. Der Pfad steht nicht im
# Argumentbereich des Loeschaufrufs, sondern im Aufruf DAVOR — ohne diesen Pfad
# verliert die Kopplung den pathlib-Fall.
RECEIVER_RE = re.compile(r"\(([^()]*)\)[\w.\s]*\.\s*\Z")


def _arg_regions(segment, api, *, lits=None):
    """Bereiche, in denen ein Pfad-Literal als ZIEL dieses Aufrufs stehen kann."""
    lo, hi = _quote_bounds(segment, api.start(), lits=lits)
    if api.group().endswith("("):
        open_at = api.end() - 1
    else:
        i = api.end()
        while i < hi and (segment[i].isalnum() or segment[i] in "_. \t"):
            i += 1
        if i >= hi or segment[i] != "(":
            return []
        open_at = i
    close_at = _closing_paren(segment, open_at, hi)
    if segment[open_at + 1:close_at].strip():
        return [(open_at, close_at)]
    receiver = RECEIVER_RE.search(segment[lo:api.start()])
    if receiver is None:
        return []
    return [(lo + receiver.start(1) - 1, lo + receiver.end(1))]


def _api_delete_hit(segment, cwd):
    """Treffer-Literal oder None.

    `braces=False`: die Shell expandiert `{a,b}` INNERHALB von Quotes gar nicht,
    das Brace-Verdachtsflag wertet aber jedes Komma als Wildcard. Unter der
    Signatur-Pfad-Kopplung kostet das Flag nichts: ein Komma in einem Literal im
    Argumentbereich ist kein Wildcard-Hinweis. Festgehalten bleibt es ueber den
    CASE `shutil.rmtree("a, b")`.

    `bare_root=False`: ein Literal `"/"`, `"."` oder `"~"` im Argumentbereich ist
    kein Treffer auf jede Wurzel — siehe `_hits_guarded`.

    Relative Literale zaehlen mit (cwd-Aufloesung wie bei `rm`); das kostet den
    Fall, dass ein Wartungs-Kommando einen relativen scripts/-Pfad als Loeschziel
    nennt, und liefert dafuer den Treffer `unlinkSync("skills/foo/SKILL.md")`."""
    hits = list(DELETE_API_RE.finditer(segment))
    if not hits:
        return None
    if len(segment) > COUPLED_SEGMENT_MAX:
        return COUPLED_TOO_LONG
    # EINMAL pro Segment, nicht pro Treffer -- s. gleichlautender Kommentar in
    # destructive-command-gate.py: _literal_matches ist ein voller Scan, und pro
    # Treffer gerufen waere dieser Kanal quadratisch und risse das 5-s-Hook-
    # Timeout.
    lits = _literal_matches(segment)
    for api in hits:
        for open_at, close_at in _arg_regions(segment, api, lits=lits):
            for m in lits:
                if m.start(1) <= open_at or m.end(1) > close_at:
                    continue
                if _hits_guarded(m.group(1), cwd, braces=False, bare_root=False):
                    return m.group(1)
    return None


def evaluate(command, cwd):
    for segment in _segments(command):
        parsed = _delete_args(segment, cwd)
        # `git -C <dir>` verschiebt den cwd fuer BEIDE Kanaele, nicht nur fuer den
        # rm-Kanal.
        literal_cwd = cwd
        if parsed is not None:
            args, effective_cwd = parsed
            literal_cwd = effective_cwd
            for token in args:
                if _hits_guarded(token, effective_cwd):
                    return segment, token
        literal = _api_delete_hit(segment, literal_cwd)
        if literal is not None:
            return segment, literal
    return None


CASES = [
    # (command, cwd, expected)
    ("rm -rf skills/foo", CLAUDE_DIR, True),
    ("rm skills/foo/SKILL.md", CLAUDE_DIR, True),
    ("rm -rf ./skills/foo", CLAUDE_DIR, True),
    ("rm -rf ~/.claude/agents/critic.md", "/tmp", True),
    ("rm -rf /Users/alice/.claude/skills", "/tmp", True),
    ("rm -rf $HOME/.claude/skills/learned", "/tmp", True),
    ("git rm -r skills/learned", CLAUDE_DIR, True),
    ("git -C /Users/alice/.claude rm agents/foo.md", "/tmp", True),
    ("cat foo.md && rm -f agents/tmp.md", CLAUDE_DIR, True),
    ("sudo rm -rf skills/foo", CLAUDE_DIR, True),
    ("git mv skills/learned/foo skills/foo", CLAUDE_DIR, False),
    ("git mv agents/a.md agents/b.md", CLAUDE_DIR, False),
    ("mv skills/learned/foo skills/foo", CLAUDE_DIR, False),
    ("grep -rn 'skills' /Users/alice/.claude/agents", CLAUDE_DIR, False),
    ("ls -la skills/", CLAUDE_DIR, False),
    ("cat agents/critic.md", CLAUDE_DIR, False),
    ("echo 'rm -rf skills/foo'", CLAUDE_DIR, False),
    ("docker run --rm -v ~/.claude/skills:/x img", CLAUDE_DIR, False),
    ("rm -rf /tmp/build", CLAUDE_DIR, False),
    ("rm -rf node_modules", CLAUDE_DIR, False),
    ("rm -rf ~/vault/skills/foo", CLAUDE_DIR, False),
    # cwd-blind waere das hier faelschlich geblockt worden —
    # ein voellig anderes Repo mit eigenem skills/-Ordner, Hook ist global registriert.
    ("rm -rf skills/foo", "/Users/alice/some-other-repo", False),
    ("rm -rf agents/foo.md", "/Users/alice/project-x/code", False),
    # Fehlender cwd im Payload -> relativer Token nicht flaggen (Fail-Richtung, s. Docstring).
    ("rm -rf skills/foo", "", False),
    # git -C ueberschreibt den Payload-cwd explizit, in beide Richtungen.
    ("git -C /Users/alice/some-other-repo rm agents/foo.md", CLAUDE_DIR, False),
    # --- ~/.claude/plans ---
    ("rm -rf plans/foo", CLAUDE_DIR, True),
    ("rm ~/.claude/plans/my-plan.md", "/tmp", True),
    ("git rm -r plans/staging", CLAUDE_DIR, True),
    ("rm -rf plans/foo", "/Users/alice/some-other-repo", False),
    ("ls -la plans/", CLAUDE_DIR, False),
    # --- ~/vault/memory + ~/vault/projects ---
    ("rm -rf memory/foo", VAULT_DIR, True),
    ("rm memory/notes/2026-08-01.md", VAULT_DIR, True),
    ("git rm projects/foo.md", VAULT_DIR, True),
    ("rm -rf ~/vault/projects", "/tmp", True),
    ("git -C /Users/alice/vault rm memory/foo.md", "/tmp", True),
    # Geschwister unter ~/vault sind NICHT gegated — nur memory/projects stehen in GUARDED_ROOTS.
    ("rm -rf ~/vault/ideas/foo", CLAUDE_DIR, False),
    ("rm -rf ideas/foo", VAULT_DIR, False),
    ("rm -rf memory/foo", "/Users/alice/some-other-repo", False),
    ("git mv projects/a.md projects/b.md", VAULT_DIR, False),
    # --- ~/project-x/knowledge ---
    ("rm -rf knowledge/foo", PROJECT_X_DIR, True),
    ("rm ~/project-x/knowledge/notes/x.md", "/tmp", True),
    ("git rm -r knowledge/archive", PROJECT_X_DIR, True),
    ("rm -rf knowledge/foo", "/Users/alice/some-other-repo", False),
    ("rm -rf /Users/alice/project-x/code/knowledge/foo", "/tmp", False),
    # --- Kommando-Pfad-Praefix und Gross-/Kleinschreibung (Token statt Segment-Text) ---
    ("/bin/rm -f ~/.claude/agents/critic.md", "/tmp", True),
    ("/opt/homebrew/bin/rm -rf skills/foo", CLAUDE_DIR, True),
    ("RM -f agents/foo.md", CLAUDE_DIR, True),
    ("/usr/bin/git -C /Users/alice/.claude rm agents/foo.md", "/tmp", True),
    ("sudo /bin/rm -rf ~/vault/memory/notes", "/tmp", True),
    # Kern: Einzeldatei ohne -r (destructive-command-gate greift nur bei force+recursive).
    ("rm -f agents/critic.md", CLAUDE_DIR, True),
    ("rm agents/critic.md", CLAUDE_DIR, True),
    ("/bin/rm ~/.claude/skills/foo/SKILL.md", "/tmp", True),
    # Over-Block-Gegenprobe: Pfad-Praefix darf ausserhalb der Wurzeln nichts blocken.
    ("/bin/rm -rf /tmp/build", CLAUDE_DIR, False),
    ("/bin/rm -rf skills/foo", "/Users/alice/some-other-repo", False),
    ("rmdir skills/foo", CLAUDE_DIR, False),
    ("/usr/bin/grep -rn rm /Users/alice/.claude/agents", CLAUDE_DIR, False),
    ("echo /bin/rm -rf ~/.claude/skills", CLAUDE_DIR, False),
    # --- git rm: Flag-Matrix, keine Regression ---
    ("git rm -f agents/foo.md", CLAUDE_DIR, True),
    ("git rm --cached agents/foo.md", CLAUDE_DIR, True),
    ("git rm -r -f --cached skills/foo", CLAUDE_DIR, True),
    ("git rm -rf skills/foo", CLAUDE_DIR, True),
    ("git rm --cached -- plans/foo.md", CLAUDE_DIR, True),
    ("git -C /Users/alice/.claude rm -r --cached skills/foo", "/tmp", True),
    ("git -C/Users/alice/.claude rm agents/foo.md", "/tmp", True),
    # `-c key=val` ist Config, kein chdir — sonst wuerde "foo=bar" als Verzeichnis
    # gelesen und der Treffer dadurch weggerechnet.
    ("git -c core.quotepath=false rm agents/foo.md", CLAUDE_DIR, True),
    ("git -C /Users/alice/.claude mv agents/a.md agents/b.md", "/tmp", False),
    ("git rm --cached agents/foo.md", "/Users/alice/some-other-repo", False),
    # --- Subshell-/Klammer-Wrapper (wie in den Geschwister-Gates) ---
    ("( rm -rf skills/foo )", CLAUDE_DIR, True),
    ("$(rm -f ~/.claude/agents/critic.md)", "/tmp", True),
    # --- VAR=wert-Praefix (wie in den Geschwister-Gates) ---
    ("FOO=1 rm -rf skills/foo", CLAUDE_DIR, True),
    ("GIT_TERMINAL_PROMPT=0 git rm agents/bar.md", CLAUDE_DIR, True),
    ("A=1 B=2 C=3 rm -rf skills/foo", CLAUDE_DIR, True),
    ("FOO= rm -rf skills/foo", CLAUDE_DIR, True),
    # Gequoteter Wert — `\S*` allein wuerde bei Leerzeichen mitten im Wert abreissen
    # und den Rest-Text als Kommando-Kopf lesen.
    ('D="a b" rm -rf skills/foo', CLAUDE_DIR, True),
    ("FOO='a b' rm -f ~/.claude/agents/critic.md", "/tmp", True),
    ('GIT_SSH_COMMAND="ssh -v" git rm agents/foo.md', CLAUDE_DIR, True),
    # VAR= + die bereits vorhandene .lower()-Normalisierung des Kommando-Kopfs.
    ("FOO=1 RM -rf skills/foo", CLAUDE_DIR, True),
    ('D="a b" RM -rf skills/foo', CLAUDE_DIR, True),
    ("FOO=1 /bin/rm -rf skills/foo", CLAUDE_DIR, True),
    # VAR= + bestehende Wrapper-/Prefix-/Ketten-Pipeline, nicht dupliziert sondern kombiniert.
    ("env FOO=1 rm -rf skills/foo", CLAUDE_DIR, True),
    ("sudo FOO=1 rm -rf skills/foo", CLAUDE_DIR, True),
    ("FOO=1 sudo rm -rf skills/foo", CLAUDE_DIR, True),
    ("( FOO=1 rm -rf skills/foo )", CLAUDE_DIR, True),
    ("$(FOO=1 rm -f ~/.claude/agents/critic.md)", "/tmp", True),
    ("cat foo.md && FOO=1 rm -f agents/tmp.md", CLAUDE_DIR, True),
    # VAR= darf das git-Token-Parsing nicht stoeren: -C bleibt chdir, -c bleibt Config.
    ("GIT_TERMINAL_PROMPT=0 git -C /Users/alice/.claude rm agents/foo.md", "/tmp", True),
    ("FOO=1 git -C/Users/alice/.claude rm agents/foo.md", "/tmp", True),
    ("FOO=1 git -c core.quotepath=false rm agents/foo.md", CLAUDE_DIR, True),
    ("FOO=1 git rm -r -f --cached skills/foo", CLAUDE_DIR, True),
    ("FOO=1 git -C /Users/alice/some-other-repo rm agents/foo.md", CLAUDE_DIR, False),
    # Kein Over-Block durch das Praefix-Stripping.
    ("FOO=1 rm -rf /tmp/build", CLAUDE_DIR, False),
    ("FOO=1 rm -rf skills/foo", "/Users/alice/some-other-repo", False),
    ("FOO=1 rm -rf ~/vault/ideas/foo", CLAUDE_DIR, False),
    ("FOO=1 git mv agents/a.md agents/b.md", CLAUDE_DIR, False),
    ("FOO=1 rmdir skills/foo", CLAUDE_DIR, False),
    ("FOO=1 ls -la skills/", CLAUDE_DIR, False),
    ("echo FOO=1 rm -rf skills/foo", CLAUDE_DIR, False),
    ("grep -rn 'FOO=1 rm -rf skills' /Users/alice/.claude", CLAUDE_DIR, False),
    ("D=skills/foo", CLAUDE_DIR, False),
    # Realistische Alltagskommandos mit VAR=-Praefix duerfen
    # nicht gegated werden — sonst waere der Gate im Bypass-Modus ein Blocker fuer
    # ganz normale Builds/Tests.
    ("FOO=1 npm run build", CLAUDE_DIR, False),
    ("NODE_ENV=production npm test", CLAUDE_DIR, False),
    ("FOO=1 git status", CLAUDE_DIR, False),
    ("VERSION=1.2 make release", CLAUDE_DIR, False),
    # `\\rm` ist die klassische Alias-Umgehung; shlex loest das Escape auf, der Kopf
    # ist danach wieder `rm`. Wert mit Slashes/`$` bricht das Stripping nicht.
    ("FOO=1 \\rm -rf skills/foo", CLAUDE_DIR, True),
    ("PATH=/usr/bin:$PATH rm -rf skills/foo", CLAUDE_DIR, True),
    # --- re.I auf den Wort-Praefixen (macOS/APFS: /usr/bin/SUDO -> /usr/bin/sudo) ---
    ("SUDO rm -rf skills/foo", CLAUDE_DIR, True),
    ("SUDO RM -rf skills/foo", CLAUDE_DIR, True),
    ("Rm -rf skills/foo", CLAUDE_DIR, True),
    ("GIT rm -r skills/learned", CLAUDE_DIR, True),
    ("Git rm agents/foo.md", CLAUDE_DIR, True),
    ("GIT -C /Users/alice/.claude rm agents/foo.md", "/tmp", True),
    ("RM -rf /tmp/build", CLAUDE_DIR, False),
    ("GIT mv skills/learned/foo skills/foo", CLAUDE_DIR, False),
    ("echo 'RM -rf skills/foo'", CLAUDE_DIR, False),
    # --- Bewusst offen gelassene Luecken: als ALLOW dokumentiert, nicht als Coverage behauptet ---
    ("bash -c 'rm -f ~/.claude/agents/critic.md'", "/tmp", False),
    ("D=skills/foo; rm -rf $D", CLAUDE_DIR, False),
    ("unlink ~/.claude/agents/critic.md", "/tmp", False),
    ("mv ~/.claude/skills/foo /tmp/abgelegt", "/tmp", False),
    ("git --work-tree=/Users/alice/vault rm memory/foo.md", CLAUDE_DIR, False),
    ("find ~/.claude/agents -name '*.md' -exec rm {} \\;", "/tmp", False),
    ('git commit -m "chore $(rm -rf ~/.claude/skills/foo)"', "/tmp", False),
    # --- Parent-Loeschung (Wurzel liegt UNTER dem Ziel) ---
    # Diese Faelle vernichten agents/skills/plans real.
    ("rm -rf ~/.claude", "/tmp", True),
    ("rm -rf /Users/alice/.claude", "/tmp", True),
    ("git -C /Users/alice/.claude rm -r .", "/tmp", True),
    ("git -C ~/.claude rm -r .", "/tmp", True),
    ("rm -rf .", CLAUDE_DIR, True),
    ("rm -rf ..", os.path.join(CLAUDE_DIR, "skills"), True),
    ("rm -rf ~", "/tmp", True),
    ("rm -rf $HOME", "/tmp", True),
    ("rm -rf /Users", "/tmp", True),
    ("rm -rf /", "/tmp", True),
    ("rm -rf ~/vault", "/tmp", True),
    ("rm -rf ~/project-x", "/tmp", True),
    ("git rm -r .", VAULT_DIR, True),
    # Over-Block-Gegenprobe: ein Parent, unter dem KEINE Wurzel liegt, bleibt frei.
    ("rm -rf .", "/Users/alice/some-other-repo", False),
    ("rm -rf ..", "/Users/alice/some-other-repo/src", False),
    ("rm -rf .", os.path.join(CLAUDE_DIR, "plugins"), False),
    ("rm -rf ~/Downloads", "/tmp", False),
    ("rm -rf /Users/alice/project-x/code/build", "/tmp", False),
    ("rm -rf ~/.claude/plugins", "/tmp", False),
    # Ein verschachteltes Verzeichnis unterhalb von ~/.claude, das selbst keine
    # Wurzel ist (hier ein Worktree-Checkout), enthaelt Kopien von
    # agents/skills/plans. Diese Kopien sind nicht die Wurzeln — ein Cleanup dort
    # darf die Parent-Richtung nicht triggern.
    ("rm -rf /Users/alice/.claude/.claude/worktrees/worktree-abc123", "/tmp", False),
    ("rm -rf skills/foo", "/Users/alice/.claude/.claude/worktrees/worktree-abc123", False),
    ("rm -rf ~/.claude/.claude", "/tmp", False),
    # --- Glob (der Hook sieht den Text VOR der Shell-Expansion) ---
    ("rm -rf ~/.claude/*", "/tmp", True),
    ("rm -rf *", CLAUDE_DIR, True),
    ("rm -rf ~/.claude/s*", "/tmp", True),
    ("rm -rf ~/.claude/p*", "/tmp", True),
    ("rm -rf skills/*", CLAUDE_DIR, True),
    ("rm -rf ~/vault/memory/*.md", "/tmp", True),
    ("rm -rf ~/.claude/*/SKILL.md", "/tmp", True),
    ("rm -rf ~/.claude/skill?", "/tmp", True),
    ("git -C /Users/alice/.claude rm -r agents/*", "/tmp", True),
    # Bewusst ein Treffer, nicht Over-Block: `*/node_modules` in ~/.claude loescht
    # u.a. skills/node_modules — also INNERHALB einer Wurzel. Deckungsgleich mit
    # der Semantik, dass `rm -rf skills/<irgendwas>` blockt.
    ("rm -rf */node_modules", CLAUDE_DIR, True),
    # Glob-Over-Block-Gegenprobe: der Glob trifft nur die Ebene, auf der er steht.
    ("rm -rf *.log", CLAUDE_DIR, False),
    ("rm -rf *", "/tmp", False),
    ("rm -rf /tmp/*", CLAUDE_DIR, False),
    ("rm -rf node_modules/*", CLAUDE_DIR, False),
    ("rm -rf ~/.claude/plugins/*", "/tmp", False),
    ("rm -rf ~/.claude/projects/*/memory", "/tmp", False),
    ("rm -rf ~/vault/ideas/*", CLAUDE_DIR, False),
    # --- Case-Bypass auf case-insensitivem APFS ---
    # `ls ~/.claude/SKILLS` listet den Inhalt von skills/.
    ("rm -rf ~/.claude/SKILLS/foo", "/tmp", True),
    ("rm -rf Skills/foo", CLAUDE_DIR, True),
    ("rm -rf AGENTS/critic.md", CLAUDE_DIR, True),
    ("rm -rf ~/.CLAUDE/skills/foo", "/tmp", True),
    ("rm -rf ~/.claude/Plans/foo", "/tmp", True),
    ("rm -rf ~/VAULT/Memory/x.md", "/tmp", True),
    ("rm -rf ~/PROJECT-X/knowledge/x.md", "/tmp", True),
    ("git -C /Users/alice/.CLAUDE rm AGENTS/foo.md", "/tmp", True),
    ("git rm -r SKILLS/learned", CLAUDE_DIR, True),
    # Case-Faltung darf ausserhalb der Wurzeln nichts blocken.
    ("rm -rf ~/VAULT/Ideas/foo", "/tmp", False),
    ("rm -rf /tmp/SKILLS/foo", CLAUDE_DIR, False),
    ("rm -rf SKILLS/foo", "/Users/alice/some-other-repo", False),
    # --- Dritte Richtung: fuehrendes `//` bleibt von normpath erhalten ---
    # `//Users/alice/.claude/skills` ist auf macOS ein lebender Pfad (gleiche Inode).
    ("rm -rf //Users/alice/.claude/skills/foo", "/tmp", True),
    ("rm -rf //Users/alice/.claude", "/tmp", True),
    ("git -C //Users/alice/.claude rm agents/foo.md", "/tmp", True),
    ("rm -rf //Users/alice/.CLAUDE/SKILLS/*", "/tmp", True),
    ("rm -rf //tmp/build", CLAUDE_DIR, False),
    # --- Brace-Expansion (die Shell expandiert `{a,b}` VOR dem Hook) ---
    # Alle folgenden Block-Faelle vernichten die Wurzeln real;
    # `fnmatch` vergleicht `{skills,agents}` literal (False).
    ("rm -rf ~/.claude/{skills,agents}", "/tmp", True),
    ("rm -rf ~/{.claude,vault}", "/tmp", True),
    ("rm -rf {skills,agents}", CLAUDE_DIR, True),
    ("rm -rf ~/.claude/{plans,agents}", "/tmp", True),
    ("rm -rf ~/{vault,project-x}", "/tmp", True),
    ("rm -rf ~/.claude/skill{s,z}", "/tmp", True),
    ("rm -rf ~/.claude/{skills,agents}/learned", "/tmp", True),
    ("git -C /Users/alice/.claude rm -r {skills,agents}", "/tmp", True),
    ("FOO=1 rm -rf ~/.claude/{skills,agents}", "/tmp", True),
    ("sudo /bin/rm -rf ~/{.claude,vault}", "/tmp", True),
    ("rm -rf ~/.CLAUDE/{SKILLS,AGENTS}", "/tmp", True),
    # Bewusst akzeptierter Over-Block, nicht als Coverage behauptet: `s*,a*` ist in
    # bash/zsh KEINE Brace-Expansion (im Glob ist `,` ein normales Zeichen) und
    # trifft real weder skills/ noch agents/; `{plugins,todos}` liegt komplett
    # ausserhalb der Wurzeln. Beide blocken trotzdem — konservative Richtung statt
    # Expansions-Simulation.
    ("rm -rf ~/.claude/s*,a*", "/tmp", True),
    ("rm -rf ~/.claude/{plugins,todos}", "/tmp", True),
    # Brace-Over-Block-Gegenproben: ausserhalb der Wurzel-KETTE blockt ein Brace nichts.
    ("rm -rf /tmp/{a,b}", CLAUDE_DIR, False),
    ("rm -rf ~/.claude/plugins/{a,b}", "/tmp", False),
    ("rm -rf ~/vault/ideas/{a,b}", "/tmp", False),
    ("rm -rf node_modules/{a,b}", CLAUDE_DIR, False),
    ("echo ~/.claude/{skills,agents}", CLAUDE_DIR, False),
    ("ls -la ~/.claude/{skills,agents}", CLAUDE_DIR, False),
    ("git mv ~/.claude/{skills,agents} /tmp/x", CLAUDE_DIR, False),
    # Der Verdacht wird NUR vom Kommando-Token scharf gestellt, nie vom cwd. Ohne
    # diese Trennung waere ein fremdes Repo mit Komma/Klammer im Verzeichnisnamen
    # wieder over-geblockt — die Fremd-Repo-Invariante. Beide Faelle sind ohne das
    # `brace_suspect`-Gating ein Fehl-Block (verifiziert).
    ("rm -rf skills/foo", "/Users/alice/some,other-repo", False),
    ("rm -rf agents/foo.md", "/Users/alice/a{x}b", False),
    # ---- scripts/ ist guarded root -------------------------------------------
    ("rm -rf scripts", CLAUDE_DIR, True),
    ("rm -rf scripts/", CLAUDE_DIR, True),
    ("rm -rf scripts/merge-gate.py", CLAUDE_DIR, True),
    ("rm -f scripts/autogate_state.py", CLAUDE_DIR, True),
    ("rm -rf ~/.claude/scripts", "/tmp", True),
    ("rm -rf /Users/alice/.claude/scripts/push-gate.py", "/tmp", True),
    ("rm -rf ~/.claude/SCRIPTS/stop-gate.py", "/tmp", True),
    ("rm -rf ~/.claude/{scripts,agents}", "/tmp", True),
    ("git rm -r scripts", CLAUDE_DIR, True),
    # Kein Over-Block: fremdes Repo mit eigenem scripts/ bleibt frei.
    ("rm -rf scripts/foo.sh", "/Users/alice/code/fremd", False),
    ("ls scripts", CLAUDE_DIR, False),
    ("grep -rn x scripts/", CLAUDE_DIR, False),
    # ---- Praefix-Formen, die den Kopf verdecken -------------------------------
    ("2>/dev/null rm -rf skills/foo", CLAUDE_DIR, True),
    ("2> /dev/null rm -rf skills/foo", CLAUDE_DIR, True),
    ("2>&1 rm -rf skills/foo", CLAUDE_DIR, True),
    ("&>out rm -rf skills/foo", CLAUDE_DIR, True),
    ("<>fileX rm -rf skills/foo", CLAUDE_DIR, True),
    ("<<<hi rm -rf skills/foo", CLAUDE_DIR, True),
    (">| out rm -rf skills/foo", CLAUDE_DIR, True),
    (">|out rm -rf skills/foo", CLAUDE_DIR, True),
    ("> out rm -rf skills/foo", CLAUDE_DIR, True),
    ("noglob rm -rf skills/foo", CLAUDE_DIR, True),
    ("nocorrect rm -rf skills/foo", CLAUDE_DIR, True),
    ("exec rm -rf skills/foo", CLAUDE_DIR, True),
    ("setsid rm -rf skills/foo", CLAUDE_DIR, True),
    ("timeout 5 rm -rf skills/foo", CLAUDE_DIR, True),
    ("timeout -k 1 5 rm -rf skills/foo", CLAUDE_DIR, True),
    ("nice -n 5 rm -rf skills/foo", CLAUDE_DIR, True),
    ("stdbuf -o0 rm -rf skills/foo", CLAUDE_DIR, True),
    ("2>/dev/null noglob rm -rf skills/foo", CLAUDE_DIR, True),
    ("noglob FOO=1 rm -rf skills/foo", CLAUDE_DIR, True),
    # Escapte/gequotete Operatorzeichen: `&` ist hier ein echter Trenner,
    # das Folgekommando laeuft (an zsh 5.9 gemessen).
    ("echo a\\>& rm -rf skills/foo", CLAUDE_DIR, True),
    ("echo \"a>\"& rm -rf skills/foo", CLAUDE_DIR, True),
    # Gegenprobe: hier IST `>&` eine Umlenkung, `rm` ist ein DATEINAME.
    ("echo hi >& rm -rf skills/foo", CLAUDE_DIR, False),
    # ---- Wort-internes `#` darf das echte Ziel nicht abschneiden -------------
    ("rm -rf a#b skills/foo", CLAUDE_DIR, True),
    ("rm -rf note#1 agents/critic.md", CLAUDE_DIR, True),
    ("rm -rf x#y ~/.claude/scripts", "/tmp", True),
    # ---- Loeschen ueber Interpreter-API (kommando-KOPF-blind) ----------------
    # Block-Seite. Jede dieser Formen muss blocken, auch wenn der Kopf nicht in
    # destructive-command-gate.INLINE_CODE_FLAGS steht.
    ("node -e 'require(\"fs\").rmSync(\"/Users/alice/.claude/agents/critic.md\")'", "/tmp", True),
    ("node -e 'require(\"fs\").rm(\"/Users/alice/.claude/skills/foo\",{recursive:true},()=>{})'", "/tmp", True),
    ("node -e 'require(\"fs\").promises.rm(\"/Users/alice/.claude/skills/foo\")'", "/tmp", True),
    ("node -e 'require(\"fs\").unlinkSync(\"skills/foo/SKILL.md\")'", CLAUDE_DIR, True),
    ("nodejs -e 'require(\"fs\").rmSync(\"/Users/alice/.claude/skills\")'", "/tmp", True),
    ("node22 -e 'require(\"fs\").rmSync(\"/Users/alice/.claude/skills/foo\",{recursive:true})'", "/tmp", True),
    ("bun -e 'require(\"fs\").rmSync(\"/Users/alice/.claude/skills/foo\")'", "/tmp", True),
    ("npx tsx -e 'require(\"fs\").rmSync(\"/Users/alice/.claude/skills\")'", "/tmp", True),
    ("deno eval 'Deno.removeSync(\"/Users/alice/.claude/skills/foo\",{recursive:true})'", "/tmp", True),
    ("python3 -c 'import shutil; shutil.rmtree(\"/Users/alice/.claude/skills/foo\")'", "/tmp", True),
    ("python3.12 -c 'import shutil; shutil.rmtree(\"/Users/alice/.claude/skills\")'", "/tmp", True),
    ("python3 -X dev -c 'import shutil; shutil.rmtree(\"/Users/alice/.claude/skills/foo\")'", "/tmp", True),
    ("python3 -W ignore -c 'import os; os.remove(\"/Users/alice/.claude/agents/critic.md\")'", "/tmp", True),
    ("uv run python -c 'import shutil; shutil.rmtree(\"/Users/alice/.claude/skills\")'", "/tmp", True),
    ("python3 -c 'from pathlib import Path; Path(\"agents/critic.md\").unlink()'", CLAUDE_DIR, True),
    ("ruby -e 'FileUtils.rm_rf(\"/Users/alice/.claude/skills/foo\")'", "/tmp", True),
    ("python3 -c 'import os; os.remove(\"~/.claude/scripts/merge-gate.py\")'", "/tmp", True),
    ("python3 -c 'import shutil; shutil.rmtree(\"/Users/alice/vault/memory\")'", "/tmp", True),
    # Heredoc-Rumpf: `_segments` trennt an `\n`, der Kopf muss nicht erkannt werden.
    ("python3 - <<PY\nimport shutil\nshutil.rmtree(\"/Users/alice/.claude/skills\")\nPY", "/tmp", True),
    # Parent-Richtung und Glob gelten hier genauso wie bei `rm`.
    ("python3 -c 'import shutil; shutil.rmtree(\"/Users/alice/.claude\")'", "/tmp", True),
    ("python3 -c 'import os; os.remove(\".\")'", CLAUDE_DIR, True),
    # Durchlass-Seite DERSELBEN Formen — eine bestandene Block-Probe belegt sie nicht.
    ("node -e 'console.log(require(\"fs\").readdirSync(\"/Users/alice/.claude/skills\"))'", "/tmp", False),
    ("python3 -c 'print(open(\"/Users/alice/.claude/agents/critic.md\").read()[:10])'", "/tmp", False),
    ("python3 -c 'import os; print(os.listdir(\"/Users/alice/.claude/skills\"))'", "/tmp", False),
    ("node -e 'require(\"fs\").rmSync(\"skills/foo\",{recursive:true})'", "/Users/alice/some-other-repo", False),
    ("python3 -c 'import shutil; shutil.rmtree(\"/tmp/build\")'", CLAUDE_DIR, False),
    ("python3 -c 'import shutil; shutil.rmtree(\"/Users/alice/vault/ideas/foo\")'", "/tmp", False),
    ("python3 -c 'print(1)'", CLAUDE_DIR, False),
    # Die Unterscheidung, an der das Muster haengt: Pfad als blankes Token
    # (Suche/Lesen) ist KEIN Treffer, Pfad als Literal in der Nutzlast schon.
    ("grep -rn rmtree /Users/alice/.claude/scripts/", "/tmp", False),
    ("grep -rn 'shutil.rmtree' /Users/alice/.claude/scripts/", "/tmp", False),
    ("rg 'unlinkSync' /Users/alice/.claude/skills", "/tmp", False),
    ("cat /Users/alice/.claude/scripts/prune-gate.py", "/tmp", False),
    # Komma-Artefakt-Gegenprobe (`braces=False` im Literal-Kanal). Der dritte
    # Fall ist der eigentliche Pin — mit `braces=True` wuerde `"a, b"` auf jeder
    # Ebene als Wildcard gelten.
    ("grep -n 'os.replace(tmp, target)' /Users/alice/project-x/code/x.py", CLAUDE_DIR, False),
    ("python3 -c 'import pathlib, shutil; print(1)'", CLAUDE_DIR, False),
    ("python3 -c 'import shutil; shutil.rmtree(\"a, b\")'", CLAUDE_DIR, False),
    # Die Brace-Deckung des rm-Kanals darf dadurch NICHT verloren gehen.
    ("rm -rf ~/.claude/{skills,agents}", "/tmp", True),
    # Nutzlast-als-TEXT bleibt Over-Block: Verb und Pfad stehen im SELBEN Aufruf.
    ("echo 'shutil.rmtree(\"/Users/alice/.claude/skills\")'", "/tmp", True),
    # ---- Kopplung Signatur <-> Pfad (Argumentbereich) -------------------------
    # Gate-PFLEGE greppt routinemaessig nach genau diesen API-Namen unter
    # scripts/ und darf nicht blocken.
    ("grep -rn 'unlink(' '/Users/alice/.claude/scripts'", "/tmp", False),
    ("grep -rn \"shutil.rmtree\" \"$HOME/.claude/scripts\"", "/tmp", False),
    ("grep -rn \"os.remove(\" \"$HOME/.claude/scripts/prune-gate.py\"", "/tmp", False),
    # Quoting-SYMMETRIE: dieselbe Handlung, ein Ergebnis. Die Entscheidung darf
    # nicht daran haengen, ob der Pfad gequotet ist.
    ("grep -rn 'unlink(' /Users/alice/.claude/scripts", "/tmp", False),
    # Loeschziel /tmp, Lesepfad unter einer Wurzel — ein Segment, zwei Aufrufe.
    ("python3 -c 'import os; os.remove(\"/tmp/x\"); "
     "print(open(\"/Users/alice/.claude/scripts/x.py\").read())'", "/tmp", False),
    ("node -e 'require(\"fs\").rmSync(\"/tmp/x\"); "
     "console.log(require(\"fs\").readFileSync(\"/Users/alice/.claude/agents/critic.md\"))'", "/tmp", False),
    # Block-Gegenprobe zu denselben Formen: Pfad IM Argumentbereich.
    ("python3 -c 'import os; os.remove(\"/Users/alice/.claude/scripts/x.py\"); print(open(\"/tmp/x\").read())'", "/tmp", True),
    # ---- Leere Komponentenliste (`all([])`) ----------------------------------
    # Literal-Kanal: `/` ist kein Treffer auf jede Wurzel. Fuer `rm` bleibt es einer.
    ("python3 -c 'import shutil; shutil.rmtree(\"/\")'", CLAUDE_DIR, False),
    ("rm -rf /", CLAUDE_DIR, True),
    ("python3 -c 'import os; os.remove(\"/tmp/x\"); print(\".\")'", CLAUDE_DIR, False),
    ("python3 -c 'import os; os.remove(\"/tmp/x\"); print(\"~\")'", CLAUDE_DIR, False),
    # Asymmetrie-Pin: als ZIEL bleiben `.`/`~` Treffer (Parent-Richtung, wie bei `rm`).
    ("python3 -c 'import shutil; shutil.rmtree(\"~\")'", "/tmp", True),
    # ---- Backtick-Literale (Template-Strings) ---------------------------------
    ("bun -e 'require(\"fs\").rmSync(`/Users/alice/.claude/skills`,{recursive:true})'", "/tmp", True),
    ("node -e 'require(\"fs\").rmSync(`/Users/alice/.claude/agents/critic.md`)'", "/tmp", True),
    ("bun -e 'require(\"fs\").unlinkSync(`skills/foo/SKILL.md`)'", CLAUDE_DIR, True),
    ("node -e 'console.log(require(\"fs\").readdirSync(`/Users/alice/.claude/skills`))'", "/tmp", False),
    ("bun -e 'require(\"fs\").rmSync(`/tmp/x`,{recursive:true})'", CLAUDE_DIR, False),
    # Empfaenger-Form (Pfad im Aufruf DAVOR) beidseitig.
    ("python3 -c 'from pathlib import Path; print(Path(\"agents/critic.md\").read_text())'", CLAUDE_DIR, False),
]

KNOWN_GAPS = [
    # Kontext-/Pfad-Aufloesung ausserhalb des Segments. Die
    # Variablen-EXPANSION bleibt offen (`$D` wird nicht aufgeloest); das
    # vorangestellte `VAR=wert`-PRAEFIX ist dagegen abgedeckt, s. CASES.
    "cd ~/.claude/skills && rm -rf foo",
    "D=skills/foo; rm -rf $D",
    "D=skills/foo rm -rf $D",
    "ls skills/foo | xargs rm -rf",
    "find skills -name '*.md' -delete",
    "find ~/.claude/agents -name '*.md' -exec rm {} \\;",
    "cd ~/vault && rm -rf memory/foo",
    # Kommando-Kopf wird nicht als rm/git erkannt (bleibt auch mit
    # Token-Erkennung und VAR=-Praefix-Stripping offen — hier steckt das Kommando
    # im Argument).
    "bash -c \"rm -f ~/.claude/agents/critic.md\"",
    # Rest-Residuum derselben Praefix-Klasse: `env -i` laesst ein Flag statt eines
    # Kommandos als Kopf zurueck (offen). `IFS=$'…'` (ANSI-C-Quoting im Wert) und
    # ein fuehrender Redirect werden erkannt und stehen hier als Pin.
    "IFS=$' ' rm -rf ~/.claude/skills/foo",
    "env -i rm -rf ~/.claude/skills/foo",
    "2>/dev/null rm -rf ~/.claude/skills/foo",
    "git commit -m \"chore $(rm -rf ~/.claude/skills/foo)\"",
    "alias del=rm; del -f ~/.claude/agents/critic.md",
    "python3 -c \"import os; os.remove('/Users/alice/.claude/agents/critic.md')\"",
    # Loeschen/Vernichten ohne rm-Verb.
    "unlink ~/.claude/agents/critic.md",
    "mv ~/.claude/skills/foo /tmp/abgelegt",
    "echo '' > ~/.claude/agents/critic.md",
    # Pfad-Aufloesung: nur `-C` gilt als chdir, Symlinks im Ziel-Pfad werden
    # normpath- statt realpath-aufgeloest.
    "git --git-dir=/Users/alice/vault/.git --work-tree=/Users/alice/vault rm memory/foo.md",
    "ln -s ~/.claude/agents /tmp/a; rm -f /tmp/a/critic.md",
    # Weitere Richtung derselben Pfad-Vergleichs-Familie, NICHT von der
    # Parent-/Case-/`//`-Behandlung geschlossen: macOS-Firmlink.
    # `/System/Volumes/Data/Users/...` ist ein lebender
    # Pfad auf dieselbe Inode wie `/Users/...`, und `realpath`
    # normalisiert ihn NICHT zurueck — ein Fix braucht eine explizite
    # Alias-Praefix-Regel oder st_dev/st_ino-Vergleich, nicht nur Textvergleich.
    "rm -rf /System/Volumes/Data/Users/alice/.claude/skills/foo",
    # Wrapper-Kommandos: `caffeinate`, `doas` und `gtimeout` stehen nicht in
    # WRAPPER_WORDS und bleiben offen; `nice`, `stdbuf`, `setsid` und `timeout`
    # stehen drin, werden erkannt und sind als Pin gelistet. Jeweils einzeln
    # verifiziert statt unter "Wrapper-Skripte" pauschalisiert.
    "nice rm -rf ~/.claude/skills/foo",
    "caffeinate rm -rf ~/.claude/skills/foo",
    "stdbuf -o0 rm -rf ~/.claude/skills/foo",
    "setsid rm -rf ~/.claude/skills/foo",
    "doas rm -rf ~/.claude/skills/foo",
    "timeout 5 rm -rf ~/.claude/skills/foo",
    "gtimeout 5 rm -rf ~/.claude/skills/foo",
    # Escaped Space im VAR-Wert: shlex loest das Escape auf, der Wert bleibt ein
    # Token, das Praefix-Stripping greift. Als Pin gelistet.
    "FOO=a\\ b rm -rf ~/.claude/skills/foo",
    # Literal-Kanal: er braucht den Pfad ALS TEXT im Kommando und eine
    # Signatur aus DELETE_API_RE. Diese drei Richtungen bleiben offen, der
    # `os.remove`-Eintrag oben ist dagegen ein Treffer.
    # Pfad per VARIABLE: Signatur und Pfad sind gekoppelt, ein per Variable
    # zusammengesetzter Pfad hat im Argumentbereich kein Literal. Eine echte
    # Luecke, die hier steht, statt als Nicht-Luecke behauptet zu werden.
    "python3 -c 'import os,shutil; shutil.rmtree(os.environ[\"HOME\"]+\"/.claude/skills\")'",
    "python3 -c 'import shutil; p=\"/Users/alice/.claude\"; shutil.rmtree(p+\"/skills\")'",
    "python3 -c 'from fs_extra import remove_it; remove_it(\"/Users/alice/.claude/skills\")'",
    "python3 /tmp/cleanup.py",
    # git-Unterkommandos, die Inhalte unter den Wurzeln vernichten, ohne `rm` zu
    # heissen — `_git_rm_args` verlangt exakt `rm` als Subkommando.
    "git clean -fd ~/.claude/skills",
    "git checkout -- ~/.claude/agents/foo.md",
    "git restore ~/.claude/agents/foo.md",
]


# --- Laengenschranke des Literal-Kanals ----------------------------------
# Drei Faelle: oberhalb fail-closed schliessen, den signaturfreien Normalfall
# NICHT anfassen, unterhalb nichts am Verhalten aendern.
CAP_CASES = (
    ("ueber der Schranke MIT Loesch-API -> Treffer (fail-closed)", True),
    ("ueber der Schranke OHNE API-Signatur -> kein Treffer", True),
    ("knapp unter der Schranke -> Verhalten unveraendert", True),
)


def _cap_selftest():
    """Rueckgabe: Zahl der Fehlschlaege."""
    failures = 0
    core = 'unlinkSync("skills/foo/SKILL.md"); '
    over = core * ((COUPLED_SEGMENT_MAX // len(core)) + 40)
    assert len(over) > COUPLED_SEGMENT_MAX

    hit = _api_delete_hit(over, CLAUDE_DIR)
    ok = hit == COUPLED_TOO_LONG
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'} [cap] {CAP_CASES[0][0]}"
          + ("" if ok else f" -> {hit!r}"))

    harmless = "echo " + "x" * (COUPLED_SEGMENT_MAX + 1000)
    hit = _api_delete_hit(harmless, CLAUDE_DIR)
    ok = hit is None
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'} [cap] {CAP_CASES[1][0]}"
          + ("" if ok else f" -> {hit!r}"))

    small = 'unlinkSync("skills/foo/SKILL.md")'
    assert len(small) < COUPLED_SEGMENT_MAX
    hit = _api_delete_hit(small, CLAUDE_DIR)
    ok = bool(hit) and hit != COUPLED_TOO_LONG
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'} [cap] {CAP_CASES[2][0]}"
          + ("" if ok else f" -> {hit!r}"))
    return failures


def selftest():
    failures = 0
    print("== Statisch (evaluate()) ==")
    for command, cwd, expected in CASES:
        hit = evaluate(command, cwd) is not None
        ok = hit == expected
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'} [{'block' if expected else 'pass '}] (cwd={cwd or '<leer>'}) {command}")
    print(f"\n{len(CASES) - failures}/{len(CASES)} OK")

    print("\n== Payload-Shape end-to-end (eigener Prozess, echtes stdin) ==")
    payload_failures = _run_payload_cases(PAYLOAD_CASES)
    print(f"\n{len(PAYLOAD_CASES) - payload_failures}/{len(PAYLOAD_CASES)} OK")
    failures += payload_failures

    print("\n== JSON-Shape end-to-end (Payload-FORM, echtes stdin) ==")
    shape_failures = _run_payload_cases(JSON_SHAPE_CASES)
    print(f"\n{len(JSON_SHAPE_CASES) - shape_failures}/{len(JSON_SHAPE_CASES)} OK")
    failures += shape_failures

    print("\n== Laengenschranke des Literal-Kanals ==")
    failures += _cap_selftest()

    print("\nBekannte Luecken (Layer, kein Zaun — bewusst nicht abgedeckt):")
    for gap in KNOWN_GAPS:
        print(f"  GAP  {gap}  -> {'erkannt' if evaluate(gap, CLAUDE_DIR) else 'nicht erkannt'}")
    total = (len(CASES) + len(PAYLOAD_CASES) + len(JSON_SHAPE_CASES)
             + len(CAP_CASES))
    print(f"\nGesamt: {total - failures}/{total} OK")
    sys.exit(1 if failures else 0)



UNPARSEABLE_REASON = (
    "[Prune-Gate] Der Hook hat kein auswertbares `tool_input.command` bekommen "
    "(fehlender oder nicht-stringartiger Wert). Ein nicht interpretierbares "
    "Kommando gilt als UNBEKANNT und wird geblockt statt durchgelassen — ein "
    "abgestuerzter PreToolUse-Hook laesst das Tool laufen (fail-open).")


def _command_of(data):
    """Kommandotext, "" wenn nichts zu pruefen ist, None = unauswertbar."""
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if command is None or command == "":
        return ""
    if not isinstance(command, str):
        return None
    return command


UNPARSEABLE_CWD_REASON = (
    "[Prune-Gate] Der Hook hat ein `cwd` bekommen, das vorhanden, aber kein "
    "String ist. Ohne auswertbares Arbeitsverzeichnis laesst sich kein "
    "relativer Loesch-Pfad gegen die geschuetzten Wurzeln aufloesen — der "
    "Payload gilt als UNBEKANNT und wird geblockt statt durchgelassen — ein "
    "TypeError aus `os.path.join` liesse den Hook abstuerzen, und ein "
    "abgestuerzter PreToolUse-Hook laesst das Tool laufen (fail-open).")


def _cwd_of(data):
    """cwd-String, "" wenn keins gesetzt ist, None = vorhanden aber unauswertbar."""
    cwd = data.get("cwd")
    if not cwd:
        return ""
    if not isinstance(cwd, str):
        return None
    return cwd


def _deny_unparseable(reason=UNPARSEABLE_REASON):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}))

# Entscheidungs-Protokoll (metrics/gates). Fail-open und bewusst defensiv
# importiert: ein fehlender oder kaputter Logger darf dieses Gate nie zum
# Absturz bringen -- ein abgestuerzter PreToolUse-Hook sperrt im schlimmsten
# Fall jeden Bash-Aufruf aus.
GATE_LOG_NAME = "prune-gate"
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
    if not isinstance(data, dict):
        log_decision(GATE_LOG_NAME, "deny", "unparseable-payload", data)
        _deny_unparseable()
        return
    command = _command_of(data)
    if command is None:
        log_decision(GATE_LOG_NAME, "deny", "unparseable-command", data)
        _deny_unparseable()
        return
    cwd = _cwd_of(data)
    if cwd is None:
        log_decision(GATE_LOG_NAME, "deny", "unparseable-cwd", data)
        _deny_unparseable(UNPARSEABLE_CWD_REASON)
        return
    hit = evaluate(command, cwd)
    if not hit:
        log_decision(GATE_LOG_NAME, "allow", "no-hit", data)
        sys.exit(0)
    mode = data.get("permission_mode", "")
    decision = "ask" if mode in ("default", "plan") else "deny"
    base = (f"[Prune-Gate] Loeschbefehl auf geschuetztes Verzeichnis erkannt (Ziel: {hit[1]}). "
            "Regel: Skills/Agents/Plaene sowie "
            "Notizen und Vault-Inhalte werden NIE autonom geloescht — immer erst fragen. ")
    if decision == "deny":
        reason = base + ("Im Auto-Approve/bypass-Modus hart geblockt. Alternativen: `git mv` in ein "
                         "Archiv, oder interaktiv (default-Modus) mit ausdruecklichem Go des Maintainers loeschen.")
    else:
        reason = base + "Vor Bestaetigung pruefen: ist die Loeschung wirklich vom Maintainer gedeckt?"
    log_decision(GATE_LOG_NAME, decision, "protected-delete", data)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason}}))


# --- Payload-Shape end-to-end, als eigener Prozess --------------------------
# Beobachtungsebene bewusst NICHT `evaluate()`: der Befund war ein ABSTURZ von
# main(), und ein abgestuerzter PreToolUse-Hook liefert kein deny — Claude Code
# laesst das Tool dann laufen. Sichtbar wird das nur, wenn Payload ueber echtes
# stdin geht und stdout UND Exit-Code geprueft werden.
# (payload, Erwartung, Notiz). Erwartung: None = keine Ausgabe (durchgelassen),
# "ask"/"deny" = Decision, "deny+cwd" = deny aus dem cwd-Guard,
# "deny+unparseable" = deny aus dem command-Guard.
# {claude} wird durch CLAUDE_DIR ersetzt.
PAYLOAD_CASES = [
    # Der Befund: vorhandener, nicht-stringartiger cwd. Beide Absturz-Pfade —
    # relativer Token (_hits_guarded) und `git -C <relativ>` (_resolve_cdirs).
    ('{"tool_input": {"command": "rm -rf skills/foo"}, "cwd": 42,'
     ' "permission_mode": "bypassPermissions"}', "deny+cwd", "cwd ist int"),
    ('{"tool_input": {"command": "rm -rf skills/foo"}, "cwd": 4.2,'
     ' "permission_mode": "bypassPermissions"}', "deny+cwd", "cwd ist float"),
    ('{"tool_input": {"command": "rm -rf skills/foo"}, "cwd": true,'
     ' "permission_mode": "bypassPermissions"}', "deny+cwd", "cwd ist bool true"),
    ('{"tool_input": {"command": "rm -rf skills/foo"}, "cwd": ["{claude}"],'
     ' "permission_mode": "bypassPermissions"}', "deny+cwd", "cwd ist Liste"),
    ('{"tool_input": {"command": "rm -rf skills/foo"}, "cwd": {"path": "{claude}"},'
     ' "permission_mode": "bypassPermissions"}', "deny+cwd", "cwd ist Objekt"),
    ('{"tool_input": {"command": "git -C sub rm agents/x.md"}, "cwd": 42,'
     ' "permission_mode": "bypassPermissions"}', "deny+cwd",
     "cwd ist int, Absturz lag in _resolve_cdirs"),
    # Der cwd-Guard steht VOR der Detektion und gilt deshalb auch fuer harmlose
    # Kommandos — bewusst: der Payload als ganzer ist nicht auswertbar.
    ('{"tool_input": {"command": "ls -la"}, "cwd": 42,'
     ' "permission_mode": "bypassPermissions"}', "deny+cwd",
     "cwd ist int, Kommando harmlos"),
    # Der cwd-Guard ist mode-BLIND (anders als die Treffer-Entscheidung): ein
    # nicht auswertbarer Payload wird auch im default-Modus geblockt, nicht nur
    # gefragt. Gleiche Linie wie der command-Guard.
    ('{"tool_input": {"command": "rm -rf skills/foo"}, "cwd": 42,'
     ' "permission_mode": "default"}', "deny+cwd", "cwd ist int, default-Modus"),
    # FALSY Nicht-Strings: Ist-Verhalten wie bisher (`or ""`) — gleichbedeutend
    # mit "kein cwd", relativer Token nicht aufloesbar, kein Flag. Hier
    # festgeschrieben, damit ein spaeterer Fix das bewusst umdrehen muss.
    ('{"tool_input": {"command": "rm -rf skills/foo"}, "cwd": null,'
     ' "permission_mode": "bypassPermissions"}', None, "cwd null (Ist-Verhalten)"),
    ('{"tool_input": {"command": "rm -rf skills/foo"}, "cwd": 0,'
     ' "permission_mode": "bypassPermissions"}', None, "cwd 0 (falsy, Ist-Verhalten)"),
    ('{"tool_input": {"command": "rm -rf skills/foo"}, "cwd": false,'
     ' "permission_mode": "bypassPermissions"}', None, "cwd false (falsy)"),
    ('{"tool_input": {"command": "rm -rf skills/foo"}, "cwd": [],'
     ' "permission_mode": "bypassPermissions"}', None, "cwd leere Liste (falsy)"),
    ('{"tool_input": {"command": "rm -rf skills/foo"}, "cwd": "",'
     ' "permission_mode": "bypassPermissions"}', None, "cwd leerer String"),
    ('{"tool_input": {"command": "rm -rf skills/foo"},'
     ' "permission_mode": "bypassPermissions"}', None, "kein cwd-Key"),
    # Regressionswaechter: echte Treffer bleiben Treffer, harmlose bleiben frei.
    ('{"tool_input": {"command": "rm -rf skills/foo"}, "cwd": "{claude}",'
     ' "permission_mode": "bypassPermissions"}', "deny", "Treffer, bypass"),
    ('{"tool_input": {"command": "rm -rf skills/foo"}, "cwd": "{claude}",'
     ' "permission_mode": "default"}', "ask", "Treffer, default"),
    ('{"tool_input": {"command": "rm -rf ~/.claude/agents/critic.md"},'
     ' "cwd": "/tmp", "permission_mode": "bypassPermissions"}', "deny",
     "Treffer ueber absoluten Pfad"),
    ('{"tool_input": {"command": "ls -la skills/"}, "cwd": "{claude}",'
     ' "permission_mode": "bypassPermissions"}', None, "harmloses Kommando"),
    ('{"tool_input": {"command": "rm -rf /tmp/build"}, "cwd": "{claude}",'
     ' "permission_mode": "bypassPermissions"}', None, "Loeschung ausserhalb der Wurzeln"),
    # Literal-Kanal auf Prozessebene — nicht nur `evaluate()`: der
    # Kanal laeuft durch neue Regex-Logik, und ein Absturz von main() liefert
    # kein deny, also laeuft das Tool.
    ('{"tool_input": {"command": "python3 -c \'import shutil; '
     'shutil.rmtree(\\"{claude}/skills/foo\\")\'"}, "cwd": "/tmp",'
     ' "permission_mode": "bypassPermissions"}', "deny",
     "Interpreter-API-Loeschung unter einer Wurzel"),
    ('{"tool_input": {"command": "grep -rn \'unlink(\' \'{claude}/scripts\'"},'
     ' "cwd": "/tmp", "permission_mode": "bypassPermissions"}', None,
     "Gate-Pflege: Lesesuche nach einer Loesch-API unter scripts/"),
    # command-Guard (hier nur bewacht).
    ('{"tool_input": {"command": 123}, "cwd": "{claude}"}', "deny+unparseable",
     "command ist int"),
    ('{"tool_input": [{"command": "rm -rf skills/foo"}], "cwd": "{claude}"}',
     "deny+unparseable", "tool_input ist Liste"),
    ('[]', "deny+unparseable", "Top-Level Liste statt dict"),
    ('{"tool_input": {}, "cwd": "{claude}"}', None, "tool_input leer"),
    # Kaputtes JSON: bestehendes, bewusstes `except: sys.exit(0)`. Nur
    # festgeschrieben, nicht bewertet und nicht geaendert.
    ('{not json', None, "kein gueltiges JSON (dokumentiert fail-open)"),
]


# --- JSON-Shape end-to-end, gleiche Mechanik, andere Frage -------------------
# PAYLOAD_CASES oben fragt: welcher TYP steht im `cwd`. Hier geht es um die FORM
# des Payloads davor — ist das stdin-JSON ueberhaupt ein Objekt, ist `tool_input`
# eines, ist `command` ein String. Das ist der Pfad von `_command_of` und dem
# `isinstance(data, dict)`-Check in main(); auch er war ein ABSTURZ (AttributeError
# auf `data.get`/`tool_input.get`), kein bewusstes Fail-open, und ein abgestuerzter
# PreToolUse-Hook laesst das Tool laufen. Deshalb dieselbe Beobachtungsebene wie
# oben: echter Subprozess, echtes stdin, Urteil aus stdout PLUS Exit-Code.
# Tupel-Format und Erwartungs-Vokabular sind identisch zu PAYLOAD_CASES
# (None / "ask" / "deny" / "deny+cwd" / "deny+unparseable"), der Runner wird
# geteilt. Faelle, die PAYLOAD_CASES bereits abdeckt, stehen hier NICHT nochmal:
# `[]` als Top-Level, `tool_input` als nicht-leere Liste, `command` als int,
# `tool_input: {}`, das kaputte JSON und die Treffer-Regressionswaechter.
JSON_SHAPE_CASES = [
    # ---- Top-Level ist kein Objekt (PAYLOAD_CASES hat davon nur `[]`) --------
    ('"rm -rf skills/foo"', "deny+unparseable", "Top-Level String"),
    ('123', "deny+unparseable", "Top-Level Zahl"),
    ('null', "deny+unparseable", "Top-Level null"),
    ('true', "deny+unparseable", "Top-Level bool"),
    ('{}', None, "Top-Level leeres Objekt (kein tool_input -> nichts zu pruefen)"),
    # ---- tool_input ist kein Objekt, TRUTHY -> Guard greift -----------------
    ('{"tool_input": "rm -rf skills/foo", "cwd": "{claude}"}', "deny+unparseable",
     "tool_input ist String"),
    ('{"tool_input": 42, "cwd": "{claude}"}', "deny+unparseable",
     "tool_input ist Zahl"),
    ('{"tool_input": true, "cwd": "{claude}"}', "deny+unparseable",
     "tool_input ist bool"),
    # ---- tool_input ist kein Objekt, FALSY -> Ist-Verhalten, nicht Guard ----
    # `data.get("tool_input") or {}` faengt diese vier VOR der isinstance-Pruefung
    # ab. Hier festgeschrieben statt gefixt: ohne `command` gibt es nichts
    # auszufuehren, aber die Zusage "nicht auswertbar ⇒ deny" gilt hier nicht.
    ('{"tool_input": [], "cwd": "{claude}"}', None,
     "tool_input leere Liste (falsy -> {} -> allow, BEFUND)"),
    ('{"tool_input": null, "cwd": "{claude}"}', None,
     "tool_input null (falsy -> {} -> allow, BEFUND)"),
    ('{"tool_input": "", "cwd": "{claude}"}', None,
     "tool_input leerer String (falsy -> {} -> allow, BEFUND)"),
    ('{"tool_input": 0, "cwd": "{claude}"}', None,
     "tool_input 0 (falsy -> {} -> allow, BEFUND)"),
    # ---- command ist kein String (PAYLOAD_CASES hat davon nur int) ----------
    ('{"tool_input": {"command": ["rm", "-rf", "skills/foo"]}, "cwd": "{claude}"}',
     "deny+unparseable", "command ist Liste"),
    ('{"tool_input": {"command": {"text": "rm -rf skills/foo"}}, "cwd": "{claude}"}',
     "deny+unparseable", "command ist Objekt"),
    ('{"tool_input": {"command": true}, "cwd": "{claude}"}', "deny+unparseable",
     "command ist bool true"),
    ('{"tool_input": {"command": 1.5}, "cwd": "{claude}"}', "deny+unparseable",
     "command ist float"),
    # Gegenrichtung zum cwd-Guard, bewusst asymmetrisch: FALSY Nicht-Strings im
    # `command` werden geblockt (`False == ""` ist False), beim `cwd` gelten sie
    # als "nicht gesetzt". Beides ist Ist-Verhalten, hier nur nagelfest gemacht.
    ('{"tool_input": {"command": false}, "cwd": "{claude}"}', "deny+unparseable",
     "command false (falsy, trotzdem deny — Asymmetrie zu cwd)"),
    ('{"tool_input": {"command": 0}, "cwd": "{claude}"}', "deny+unparseable",
     "command 0 (falsy, trotzdem deny)"),
    ('{"tool_input": {"command": []}, "cwd": "{claude}"}', "deny+unparseable",
     "command leere Liste (falsy, trotzdem deny)"),
    # Die beiden legitimen "nichts zu pruefen"-Formen duerfen NICHT mitblocken —
    # sonst blockte der Hook jeden Tool-Call ohne Kommando.
    ('{"tool_input": {"command": null}, "cwd": "{claude}"}', None,
     "command null (explizit nichts zu pruefen)"),
    ('{"tool_input": {"command": ""}, "cwd": "{claude}"}', None,
     "command leerer String"),
    ('{"tool_input": {"command": "echo hallo"}, "cwd": "{claude}"}', None,
     "harmloses Kommando ohne Treffer"),
    # ---- Reihenfolge der Guards untereinander -------------------------------
    # Beide Guards wuerden greifen; der command-Guard steht in main() zuerst.
    # Ohne diesen Fall koennte eine spaetere Umsortierung unbemerkt durchgehen.
    ('{"tool_input": {"command": 123}, "cwd": 42}', "deny+unparseable",
     "command UND cwd unauswertbar -> command-Guard gewinnt"),
    # ---- Der Shape-Guard ist mode-BLIND (wie der cwd-Guard) -----------------
    ('{"tool_input": {"command": 123}, "cwd": "{claude}",'
     ' "permission_mode": "default"}', "deny+unparseable",
     "Shape-Guard blockt auch im default-Modus (kein ask)"),
    ('{"tool_input": ["x"], "permission_mode": "plan"}', "deny+unparseable",
     "Shape-Guard blockt auch im plan-Modus"),
    # ---- permission_mode-FORM bei echtem Treffer ----------------------------
    # Nur `default`/`plan` fuehren zu `ask`; jede andere Form — fehlend, null,
    # nicht-String, unbekannt — faellt fail-closed auf deny. Ohne diese Faelle
    # waere ein Absturz oder ein stilles `ask` im bypass-Modus untestbar.
    ('{"tool_input": {"command": "rm -rf skills/foo"}, "cwd": "{claude}"}',
     "deny", "Treffer ohne permission_mode -> fail-closed deny"),
    ('{"tool_input": {"command": "rm -rf skills/foo"}, "cwd": "{claude}",'
     ' "permission_mode": null}', "deny", "Treffer, permission_mode null"),
    ('{"tool_input": {"command": "rm -rf skills/foo"}, "cwd": "{claude}",'
     ' "permission_mode": 42}', "deny",
     "Treffer, permission_mode nicht-String (kein Absturz)"),
    ('{"tool_input": {"command": "rm -rf skills/foo"}, "cwd": "{claude}",'
     ' "permission_mode": "plan"}', "ask", "Treffer, plan-Modus -> ask"),
    ('{"tool_input": {"command": "rm -rf skills/foo"}, "cwd": "{claude}",'
     ' "permission_mode": "acceptEdits"}', "deny",
     "Treffer, acceptEdits -> deny"),
    ('{"tool_input": {"command": "rm -rf skills/foo"}, "cwd": "{claude}",'
     ' "permission_mode": "wasAuchImmer"}', "deny",
     "Treffer, unbekannter Modus -> fail-closed deny"),
    # ---- stdin gar nicht als JSON lesbar ------------------------------------
    # Leeres stdin ist die haeufigste dieser Formen (Hook ohne Payload) und laeuft
    # unter dem pauschalen `except: sys.exit(0)` mit — hier explizit
    # festgeschrieben, nicht bewertet.
    ('', None, "leeres stdin (dokumentiert fail-open)"),
    ('   ', None, "nur Whitespace (dokumentiert fail-open)"),
]


def _run_payload_case(payload):
    proc = subprocess.run([sys.executable, os.path.abspath(__file__)],
                          input=payload, capture_output=True, text=True,
                          timeout=20)
    text = proc.stdout.strip()
    if not text:
        return None, proc.returncode
    try:
        out = json.loads(text)["hookSpecificOutput"]
    except Exception:
        return "<unlesbare Ausgabe>", proc.returncode
    got = out.get("permissionDecision")
    reason = out.get("permissionDecisionReason", "")
    if got == "deny" and UNPARSEABLE_CWD_REASON in reason:
        got = "deny+cwd"
    elif got == "deny" and UNPARSEABLE_REASON in reason:
        got = "deny+unparseable"
    return got, proc.returncode


def _run_payload_cases(cases):
    failures = 0
    for payload, expected, note in cases:
        payload = payload.replace("{claude}", CLAUDE_DIR)
        got, code = _run_payload_case(payload)
        ok = got == expected and code == 0
        failures += 0 if ok else 1
        detail = "" if ok else f"  (got={got!r} rc={code})"
        print(f"{'PASS' if ok else 'FAIL'} [{expected or 'pass '}] {note}: "
              f"{payload}{detail}")
    return failures


if __name__ == "__main__":
    main()
