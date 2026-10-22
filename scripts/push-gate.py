#!/usr/bin/env python3
# PreToolUse hook for Bash: guards `git push` to main/master (explicit refspecs, force variants,
# and implicit pushes of the checked-out branch). Mode-aware: ask in default/plan mode, deny in
# auto-approve modes. It matches command text, so it is a layer, not a sandbox; see KNOWN_GAPS.
"""push-gate.py — PreToolUse Bash-Gate gegen `git push` auf main/master.

Ein Direkt-Push auf main/master umgeht den Review-Schritt: Aenderungen gehen
ueber einen eigenen Branch und einen PR, der Merge-Klick bleibt beim
Maintainer. Dieser Gate sperrt Direkt-Pushes auf main/master. Im
Auto-Approve-Modus ist weder eine permissions.ask-Regel noch ein Hook-`ask`
eine verlässliche Sperre — nur `deny` hält. Deshalb mode-aware, nach dem
Muster von prune-gate.py:
- default / plan                                     -> ask  (Prompt an den Maintainer)
- acceptEdits / bypassPermissions / unbekannter Mode -> deny (fail-closed)

Anders als prune-gate.py ist dieser Gate NICHT auf ~/.claude gescoped: ein Push
auf main/master ist in JEDEM Repo riskant. Es gibt hier kein "falsches Repo" —
kein CLAUDE_DIR-Scoping, keine cwd-Blindheits-Problematik in dem Sinne.

Abdeckung (auch hinter sudo/env/xargs, in ;/&&/||/|-Ketten, in `(...)`, mit
`git -C <dir>` und anderen git-Globals):
- explizite Refspec: `git push origin main`, `-u origin master`,
  `HEAD:main`, `refs/heads/main`, `+main` (Force-Refspec), `:main`
  (Remote-Branch löschen), `--delete main`
- Force-Varianten: `--force`, `-f`, Cluster wie `-fu`, `--force-with-lease`,
  `--force-if-includes` — im Reason-Text gesondert als Force-Push benannt,
  Block/Ask-Logik identisch
- impliziter Push (`git push`, `git push origin`, `git push origin HEAD`):
  der tatsächlich ausgecheckte Branch wird per Subprozess ermittelt
  (`git -C <effektives-cwd> rev-parse --abbrev-ref HEAD`, 2s Timeout) — und
  NUR dann geflaggt, wenn er main/master ist. Der Subprozess läuft
  ausschließlich auf diesem Pfad; steht der Branch explizit im Befehl, ist
  kein Subprozess nötig (Fast-Path).
- `--all` / `--mirror`: pusht alle lokalen Branches, also auch main/master —
  ein Subprozess prüft, ob das Repo überhaupt main/master hat.

Fail-safe: schlägt der Subprozess fehl, läuft in den Timeout, fehlt `cwd` im
Payload oder ist HEAD detached -> NICHT blocken. Gleiche Fehlerrichtung wie in
prune-gate.py: Unsicherheit löst kein Blocken aus. `--dry-run`/`-n` pusht
nichts und wird durchgelassen. Nicht-Push-Kommandos (`git fetch`, `git pull`,
`git log main`, `git diff main..HEAD`, `git checkout main`) lösen nie aus.

Grenze: Blacklist über Kommandotext — ein Layer, kein Zaun, strukturell nicht
schließbar (gleiches Restrisiko wie beim git-add-Blocker). NICHT erkannt:
`git push` über ein Alias oder eine Shell-Funktion (`gp origin main`),
`GIT_DIR=`/`--git-dir=`-Umlenkung auf ein anderes Repo (das effektive Repo
wird nur über `git -C`/cwd aufgelöst), Push in einem Quoting das die
Segment-Zerlegung verschluckt (`bash -c "git push origin main"`), ein
konfiguriertes `remote.<name>.push`/`push.default`, das einen anderen Branch
auf main mappt, Push über gh/API (`gh api -X PATCH .../git/refs/heads/main`),
Push aus einem Python-/Node-Skript, und jeder Push außerhalb des Bash-Tools
(IDE, GUI-Client, eigenes Terminal, andere Session). Echte Absicherung bleibt
Branch-Protection auf dem Remote.
"""
import json
import os
import re
import shlex
import subprocess
import sys

PROTECTED = ("main", "master")
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
# _strip_control_flow). Im git-add-commit-gate haengen Testfaelle daran
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
PUSH_VALUE_FLAGS = ("-o", "--push-option", "--repo", "--receive-pack", "--exec")
GIT_TIMEOUT = 2.0


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


def _parse_git_push(segment):
    tokens = _strip_prefixes(_tokens(_strip_wrappers(segment)))
    if not tokens or os.path.basename(tokens[0]).lower() != "git":
        return None
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
    if i >= len(tokens) or tokens[i] != "push":
        return None
    return cdirs, tokens[i + 1:]


def _parse_push_args(args):
    flags = {"force": False, "delete": False, "dry_run": False, "all_refs": False}
    positionals = []
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--":
            positionals.extend(args[i + 1:])
            break
        if token in PUSH_VALUE_FLAGS:
            i += 2
            continue
        if token.startswith("--"):
            name = token.split("=", 1)[0]
            if name in ("--force", "--force-with-lease", "--force-if-includes"):
                flags["force"] = True
            elif name == "--delete":
                flags["delete"] = True
            elif name == "--dry-run":
                flags["dry_run"] = True
            elif name in ("--all", "--mirror"):
                flags["all_refs"] = True
            i += 1
            continue
        if token.startswith("-") and len(token) > 1:
            letters = token[1:]
            if "f" in letters:
                flags["force"] = True
            if "d" in letters:
                flags["delete"] = True
            if "n" in letters:
                flags["dry_run"] = True
            if letters.endswith("o"):
                i += 1
            i += 1
            continue
        positionals.append(token)
        i += 1
    return flags, positionals


def _refspec_dst(spec):
    candidate = spec.strip().strip("'\"")
    forced = candidate.startswith("+")
    candidate = candidate.lstrip("+")
    if ":" in candidate:
        src, dst = candidate.split(":", 1)
        deleted = src == ""
    else:
        dst, deleted = candidate, False
    if dst.startswith("refs/heads/"):
        dst = dst[len("refs/heads/"):]
    return (dst or None), forced, deleted


def _resolve_cwd(cdirs, cwd):
    # git -C <dir> setzt das Arbeitsverzeichnis fuer DIESEN Befehl explizit und
    # ueberschreibt den Payload-cwd; mehrere -C sind relativ verkettet.
    result = cwd
    for raw in cdirs:
        d = os.path.expanduser(os.path.expandvars(raw.strip("'\"")))
        result = d if os.path.isabs(d) else (os.path.join(result, d) if result else "")
    return result


def _git(cwd, *args):
    if not cwd or not os.path.isdir(cwd):
        return None
    try:
        proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                              text=True, timeout=GIT_TIMEOUT)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _current_branch(cwd):
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    return branch if branch and branch != "HEAD" else None


def _protected_branch_in_repo(cwd):
    out = _git(cwd, "for-each-ref", "--format=%(refname:short)",
               "refs/heads/main", "refs/heads/master")
    if not out:
        return None
    for line in out.splitlines():
        if line.strip() in PROTECTED:
            return line.strip()
    return None


def evaluate(command, cwd):
    for segment in _segments(command):
        parsed = _parse_git_push(segment)
        if parsed is None:
            continue
        cdirs, args = parsed
        flags, positionals = _parse_push_args(args)
        if flags["dry_run"]:
            continue
        effective_cwd = _resolve_cwd(cdirs, cwd)
        refspecs = positionals[1:]
        branch = how = None
        for spec in refspecs:
            dst, forced, deleted = _refspec_dst(spec)
            if dst is None:
                continue
            if dst in ("HEAD", "@"):
                current = _current_branch(effective_cwd)
                if current in PROTECTED:
                    branch, how = current, "HEAD-Refspec auf den aktuellen Branch"
            elif dst in PROTECTED:
                branch, how = dst, "explizites Ziel im Befehl"
            if branch:
                flags["force"] = flags["force"] or forced
                flags["delete"] = flags["delete"] or deleted
                break
        if not branch and not refspecs:
            if flags["all_refs"]:
                found = _protected_branch_in_repo(effective_cwd)
                if found:
                    branch, how = found, "--all/--mirror pusht alle lokalen Branches"
            else:
                current = _current_branch(effective_cwd)
                if current in PROTECTED:
                    branch, how = current, "impliziter Push, aktuell ausgecheckter Branch"
        if branch:
            return {"segment": segment, "branch": branch, "how": how,
                    "force": flags["force"], "delete": flags["delete"]}
    return None


# (command, cwd, expected) — kein Repo noetig, Branch steht explizit im Befehl
# oder der Befehl ist gar kein Push.
CASES = [
    ("git push origin main", "/tmp", True),
    ("git push origin master", "/tmp", True),
    ("git push -u origin main", "/tmp", True),
    ("git push --set-upstream origin main", "/tmp", True),
    ("git push origin HEAD:main", "/tmp", True),
    ("git push origin HEAD:refs/heads/master", "/tmp", True),
    ("git push origin refs/heads/main", "/tmp", True),
    ("git push --force origin main", "/tmp", True),
    ("git push -f origin master", "/tmp", True),
    ("git push -fu origin main", "/tmp", True),
    ("git push --force-with-lease origin main", "/tmp", True),
    ("git push --force-with-lease=main origin main", "/tmp", True),
    ("git push origin +main", "/tmp", True),
    ("git push origin :main", "/tmp", True),
    ("git push origin --delete main", "/tmp", True),
    ("git push --delete origin master", "/tmp", True),
    ("git push -o ci.skip origin main", "/tmp", True),
    ("npm test && git push origin main", "/tmp", True),
    ("git commit -m x; git push origin main", "/tmp", True),
    ("sudo git push origin main", "/tmp", True),
    ("(git push origin main)", "/tmp", True),
    ("/usr/bin/git push origin main", "/tmp", True),
    ("git -C /Users/alice/code/foo push origin main", "/tmp", True),
    ("git --no-pager push origin main", "/tmp", True),
    ("git -c commit.gpgsign=false push origin master", "/tmp", True),
    ("git push origin main | tee /tmp/log", "/tmp", True),
    # Kein Treffer: anderer Branch, kein Push, oder Push ohne Wirkung.
    ("git push origin task/foo-bar", "/tmp", False),
    ("git push origin feature/x", "/tmp", False),
    ("git push -u origin task/push-gate", "/tmp", False),
    ("git push --force origin task/foo", "/tmp", False),
    ("git push origin HEAD:task/foo", "/tmp", False),
    ("git push origin mainline", "/tmp", False),
    ("git push origin task/main", "/tmp", False),
    ("git push origin main:task/foo", "/tmp", False),
    ("git push --dry-run origin main", "/tmp", False),
    ("git push -n origin main", "/tmp", False),
    ("git fetch origin main", "/tmp", False),
    ("git pull origin main", "/tmp", False),
    ("git log main..HEAD", "/tmp", False),
    ("git diff main..HEAD", "/tmp", False),
    ("git checkout main", "/tmp", False),
    ("git merge main", "/tmp", False),
    ("git branch -D main", "/tmp", False),
    ("git rebase main", "/tmp", False),
    ("gh pr create --base main --head task/foo", "/tmp", False),
    ("echo 'git push origin main'", "/tmp", False),
    ("grep -rn 'git push origin main' scripts/", "/tmp", False),
    # Fehlender cwd -> impliziter Push nicht aufloesbar -> nicht flaggen.
    ("git push", "", False),
    ("git push origin", "", False),
    # --- Case-Insensitivity (macOS/APFS: GIT/Git treffen dasselbe Binary) ---
    ("GIT push origin main", "/tmp", True),
    ("Git push origin main", "/tmp", True),
    ("gIt push origin master", "/tmp", True),
    ("/opt/homebrew/bin/GIT push origin main", "/tmp", True),
    ("GIT push --force origin master", "/tmp", True),
    ("GIT push origin HEAD:main", "/tmp", True),
    ("SUDO GIT push origin main", "/tmp", True),
    ("npm test && GIT push origin main", "/tmp", True),
    # VAR=wert-Praefix: allein UND kombiniert mit Case-Variation.
    ("GIT_TERMINAL_PROMPT=0 git push origin main", "/tmp", True),
    ("GIT_TERMINAL_PROMPT=0 GIT push origin main", "/tmp", True),
    ('GIT_SSH_COMMAND="ssh -v" GIT push origin main', "/tmp", True),
    ("GIT_SSH_COMMAND='ssh -v' Git push origin master", "/tmp", True),
    ("A=1 B=2 GIT push origin main", "/tmp", True),
    # Kein Over-Block durch die Normalisierung.
    ("GIT push origin task/foo", "/tmp", False),
    ("GIT push --dry-run origin main", "/tmp", False),
    ("GIT fetch origin main", "/tmp", False),
    ("echo 'GIT push origin main'", "/tmp", False),
    ("grep -rn 'GIT push origin main' scripts/", "/tmp", False),
    # ---- Praefixe, die den Kommandokopf verdecken ---------------------------
    # Alle gegen zsh 5.9 mit Sentinel-Skript gemessen (das Folgekommando lief
    # jedes Mal).
    ("2>/dev/null git push --force origin main", "/tmp", True),
    ("2> /dev/null git push --force origin main", "/tmp", True),
    ("2>&1 git push --force origin main", "/tmp", True),
    (">&2 git push --force origin main", "/tmp", True),
    ("&>out git push --force origin main", "/tmp", True),
    ("> out git push --force origin main", "/tmp", True),
    (">>out git push --force origin main", "/tmp", True),
    ("<>fileX git push --force origin main", "/tmp", True),
    ("<<<hi git push --force origin main", "/tmp", True),
    (">| out git push --force origin main", "/tmp", True),
    (">|out git push --force origin main", "/tmp", True),
    (">>|out git push --force origin main", "/tmp", True),
    ("noglob git push --force origin main", "/tmp", True),
    ("nocorrect git push --force origin main", "/tmp", True),
    ("exec git push --force origin main", "/tmp", True),
    ("setsid git push --force origin main", "/tmp", True),
    ("timeout 5 git push --force origin main", "/tmp", True),
    ("timeout -k 1 5 git push --force origin main", "/tmp", True),
    ("nice git push --force origin main", "/tmp", True),
    ("nice -n 5 git push --force origin main", "/tmp", True),
    ("stdbuf -o0 git push --force origin main", "/tmp", True),
    ("2>/dev/null noglob git push --force origin main", "/tmp", True),
    # Escaptes/gequotetes Operatorzeichen: `&` ist hier ein echter Trenner.
    ("echo a\\>& git push --force origin main", "/tmp", True),
    ('echo "a>"& git push --force origin main', "/tmp", True),
    # Gegenprobe: hier IST `>&` eine Umlenkung, `git` ist ein DATEINAME.
    ("echo hi >& git push --force origin main", "/tmp", False),
    # Kein comments=True: `#` mitten im Wort schneidet nichts ab.
    # `task/a#b` waere hier KEIN Treffer — dieser Gate schuetzt nur main/master.
    # Der Unterschied zeigt sich an einem
    # `-c`-Wert mit `#`: mit comments=True endete das Tokenizing dort, das Verb
    # `push` war weg und der Force-Push auf main lief durch.
    ("git -c http.extraHeader=x#y push --force origin main", "/tmp", True),
    ("git push --force origin task/a#b", "/tmp", False),
    ("echo '# git push --force origin main'", "/tmp", False),
]

# (command, branches, cwd_mode, expected) — braucht ein echtes Repo.
# branches[0] = Branch des Initial-Commits, weitere via `checkout -b`.
# cwd_mode "repo" = cwd ist das Test-Repo, "outside" = cwd ist ein Nicht-Repo
# (dann muss {repo} im Befehl stehen, sonst testet der Fall den Fail-safe).
REPO_CASES = [
    ("git push", ("main",), "repo", True),
    ("git push", ("master",), "repo", True),
    ("git push origin", ("main",), "repo", True),
    ("git push -u origin", ("main",), "repo", True),
    ("git push --force", ("main",), "repo", True),
    ("git push origin HEAD", ("main",), "repo", True),
    ("git push", ("main", "task/push-gate"), "repo", False),
    ("git push origin", ("main", "feature/x"), "repo", False),
    ("git push origin HEAD", ("main", "task/push-gate"), "repo", False),
    ("git push --all origin", ("main", "task/push-gate"), "repo", True),
    ("git push --mirror origin", ("main", "task/push-gate"), "repo", True),
    ("git push --all origin", ("task/push-gate",), "repo", False),
    ("git -C {repo} push", ("main",), "outside", True),
    ("git -C {repo} push", ("main", "task/push-gate"), "outside", False),
    # Fail-safe: cwd ist kein Repo, kein -C -> keine Aufloesung -> kein Block.
    ("git push", ("main",), "outside", False),
]

KNOWN_GAPS = [
    "alias gp='git push'; gp origin main",
    "bash -c \"git push origin main\"",
    "gh api -X PATCH repos/o/r/git/refs/heads/main -f sha=$SHA",
    "python -c \"import subprocess; subprocess.run(['git','push'])\"",
]


def _make_repo(root, branches):
    env = dict(os.environ, HOME=root, GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")

    def run(*args):
        subprocess.run(["git", "-C", root, *args], env=env, check=True,
                       capture_output=True, timeout=10)

    subprocess.run(["git", "init", "-q", root], env=env, check=True,
                   capture_output=True, timeout=10)
    run("symbolic-ref", "HEAD", f"refs/heads/{branches[0]}")
    run("commit", "-q", "--allow-empty", "--no-verify", "-m", "init")
    for branch in branches[1:]:
        run("checkout", "-q", "-b", branch)


def _run_repo_cases():
    import shutil
    import tempfile
    failures = 0
    for command, branches, cwd_mode, expected in REPO_CASES:
        root = tempfile.mkdtemp(prefix="push-gate-repo-")
        outside = tempfile.mkdtemp(prefix="push-gate-out-")
        try:
            _make_repo(root, branches)
            cmd = command.replace("{repo}", root)
            cwd = root if cwd_mode == "repo" else outside
            hit = evaluate(cmd, cwd) is not None
        finally:
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(outside, ignore_errors=True)
        ok = hit == expected
        failures += 0 if ok else 1
        label = "block" if expected else "pass "
        print(f"{'PASS' if ok else 'FAIL'} [{label}] (repo branches={'/'.join(branches)},"
              f" cwd={cwd_mode}) {command}")
    return failures


def selftest():
    failures = 0
    print("== Statisch (kein Repo noetig) ==")
    for command, cwd, expected in CASES:
        hit = evaluate(command, cwd) is not None
        ok = hit == expected
        failures += 0 if ok else 1
        label = "block" if expected else "pass "
        print(f"{'PASS' if ok else 'FAIL'} [{label}] (cwd={cwd or '<leer>'}) {command}")
    print(f"\n{len(CASES) - failures}/{len(CASES)} OK")

    print("\n== Mit echtem temporaerem Git-Repo (impliziter Branch) ==")
    repo_failures = _run_repo_cases()
    print(f"\n{len(REPO_CASES) - repo_failures}/{len(REPO_CASES)} OK")
    failures += repo_failures

    print("\n== Payload-Pfad (main(), nicht nur evaluate()) ==")
    payload_failures = _run_payload_cases()
    print(f"\n{len(PAYLOAD_CASES) - payload_failures}/{len(PAYLOAD_CASES)} OK")
    failures += payload_failures

    print("\n== JSON-Shape-Guard end-to-end (eigener Prozess, echtes stdin) ==")
    shape_failures = _run_shape_cases()
    print(f"\n{len(SHAPE_CASES) - shape_failures}/{len(SHAPE_CASES)} OK")
    failures += shape_failures

    print("\nBekannte Luecken (Layer, kein Zaun — bewusst nicht abgedeckt):")
    for gap in KNOWN_GAPS:
        print(f"  GAP  {gap}  -> {'erkannt' if evaluate(gap, '/tmp') else 'nicht erkannt'}")
    total = len(CASES) + len(REPO_CASES) + len(PAYLOAD_CASES) + len(SHAPE_CASES)
    print(f"\nGesamt: {total - failures}/{total} OK")
    sys.exit(1 if failures else 0)



UNPARSEABLE_REASON = (
    "[Push-Gate] Der Hook hat kein auswertbares `tool_input.command` bekommen "
    "(fehlender oder nicht-stringartiger Wert). Ein nicht interpretierbares "
    "Kommando gilt als UNBEKANNT und wird geblockt statt durchgelassen — ohne "
    "diese Pruefung wuerde der Hook hier mit einer Exception sterben, und ein "
    "abgestuerzter PreToolUse-Hook laesst das Tool laufen (fail-open).")

UNPARSEABLE_CWD_REASON = (
    "[Push-Gate] Der Hook hat ein `cwd` bekommen, das kein String ist. Ein "
    "ungeprueft weitergereichter Wert wuerde in os.path.join()/os.path.isdir() "
    "den Hook mit einem TypeError umreissen (gemessen: `cwd` als Zahl zusammen mit "
    "`git -C <dir> push`, und `cwd` als Liste bei implizitem Push) — ein "
    "abgestuerzter PreToolUse-Hook liefert kein deny und laesst das Tool laufen "
    "(fail-open). Ein nicht auswertbares Arbeitsverzeichnis gilt darum als "
    "UNBEKANNT und wird geblockt, gleiche Fehlerrichtung wie beim Kommandotext.")


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


def _cwd_of(data):
    """Arbeitsverzeichnis, "" wenn keines geliefert wurde, None = unauswertbar."""
    cwd = data.get("cwd")
    if cwd is None or cwd == "":
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
GATE_LOG_NAME = "push-gate"
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
    kind = "Force-Push" if hit["force"] else ("Remote-Branch-Loeschung" if hit["delete"] else "Push")
    base = (f"[Push-Gate] {kind} auf `{hit['branch']}` erkannt ({hit['how']}). "
            "Direkt-Push auf den geschuetzten Branch ist nicht vorgesehen; "
            "Aenderungen laufen ueber einen eigenen Branch und einen PR — "
            "Merge-Klick bleibt beim Maintainer. ")
    if decision == "deny":
        reason = base + ("Im Auto-Approve/bypass-Modus hart geblockt. Stattdessen: auf "
                         "einen eigenen Branch pushen und einen PR aufmachen.")
    else:
        reason = base + "Vor Bestaetigung pruefen: ist der Direkt-Push wirklich vom Maintainer gedeckt?"
    log_decision(GATE_LOG_NAME, decision,
                 "force-push" if hit["force"] else
                 ("branch-delete" if hit["delete"] else "protected-push"), data)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason}}))


PAYLOAD_CASES = [
    ('{"tool_input": "kein dict"}', "deny"),
    ('{"tool_input": {"command": ["git", "push", "origin", "main"]}}', "deny"),
    ('{"tool_input": {"command": 42}}', "deny"),
    ('["kein", "dict"]', "deny"),
    ('"kein dict"', "deny"),
    ('{"tool_input": null}', None),
    ('{"tool_input": {}}', None),
    ('{"tool_input": {"command": ""}}', None),
    ('{"tool_input": {"command": "git status"}, "cwd": "/tmp"}', None),
    ('{"tool_input": {"command": "git push origin main"}, "cwd": "/tmp",'
     ' "permission_mode": "bypassPermissions"}', "deny"),
    ('{"tool_input": {"command": "git push origin main"}, "cwd": "/tmp",'
     ' "permission_mode": "default"}', "ask"),
    ('kein json', None),
    # Nicht-stringartiges `cwd`. Ohne den _cwd_of-Guard sind das keine FAILs,
    # sondern ein Abbruch des Selftests: der TypeError fliegt aus main() heraus
    # (gemessen — `git -C sub push` starb in
    # os.path.join, der implizite Push in os.path.isdir). Im echten Hook heisst
    # derselbe Absturz: kein deny, Tool laeuft (fail-open).
    ('{"tool_input": {"command": "git -C sub push"}, "cwd": 42}', "deny"),
    ('{"tool_input": {"command": "git push"}, "cwd": ["/tmp"]}', "deny"),
    ('{"tool_input": {"command": "git push"}, "cwd": {"path": "/tmp"}}', "deny"),
    ('{"tool_input": {"command": "git push"}, "cwd": true}', "deny"),
    # Auch ohne Push-Bezug: unauswertbarer Payload wird geblockt, nicht
    # stillschweigend durchgewinkt — gleiche Linie wie beim Kommandotext.
    ('{"tool_input": {"command": "ls -la"}, "cwd": 42}', "deny"),
    # Regressionswaechter: fehlendes/leeres cwd bleibt der
    # "nicht aufloesbar"-Pfad, kein deny.
    ('{"tool_input": {"command": "git push"}, "cwd": null}', None),
    ('{"tool_input": {"command": "git push"}, "cwd": ""}', None),
    ('{"tool_input": {"command": "git push origin main"}, "cwd": null,'
     ' "permission_mode": "default"}', "ask"),
]


def _run_payload_cases():
    import contextlib
    import io
    failures = 0
    for payload, expected in PAYLOAD_CASES:
        out = io.StringIO()
        real_stdin, real_argv = sys.stdin, sys.argv
        sys.stdin, sys.argv = io.StringIO(payload), [real_argv[0]]
        try:
            with contextlib.redirect_stdout(out):
                try:
                    main()
                except SystemExit:
                    pass
        finally:
            sys.stdin, sys.argv = real_stdin, real_argv
        text = out.getvalue().strip()
        got = json.loads(text)["hookSpecificOutput"]["permissionDecision"] if text else None
        ok = got == expected
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'} [{expected or 'pass '}] {payload}")
    return failures


# --- JSON-Shape-Guard end-to-end, als eigener Prozess -----------------------
# Unterschied zu PAYLOAD_CASES: dort wird main() IN diesem Prozess mit
# gepatchtem sys.stdin/sys.argv aufgerufen und SystemExit abgefangen. Damit
# bleibt genau das unsichtbar, was im echten Hook-Aufruf zaehlt: ob der Prozess
# sauber terminiert. Ein PreToolUse-Hook, der mit einer Exception stirbt,
# liefert kein deny — Claude Code laesst das Tool dann laufen (fail-open, genau
# der Zustand, gegen den UNPARSEABLE_REASON und UNPARSEABLE_CWD_REASON
# argumentieren). Deshalb hier: eigener Interpreter auf DIESE Datei, Payload
# ueber echtes stdin, Auswertung von stdout UND Exit-Code.
#
# Shape-Ueberschneidung mit PAYLOAD_CASES ist teils gewollt (dieselbe Form,
# andere Beobachtungsebene) — in der Notiz jeweils als "= PAYLOAD" markiert.
# "nur SHAPE" markiert Formen, die in PAYLOAD_CASES ueberhaupt nicht vorkommen.
#
# (payload, Erwartung, Notiz). Erwartung: None = keine Ausgabe (durchgelassen),
# "ask"/"deny" = Decision aus der regulaeren Push-Erkennung,
# "deny+unparseable" = deny mit UNPARSEABLE_REASON (Kommando-Guard),
# "deny+cwd" = deny mit UNPARSEABLE_CWD_REASON (cwd-Guard).
SHAPE_CASES = [
    ('[]', "deny+unparseable", "nur SHAPE: Top-Level LEERE Liste"),
    ('"x"', "deny+unparseable", "= PAYLOAD Top-Level String"),
    ('{"tool_input": [{"command": "git push origin main"}]}',
     "deny+unparseable", "nur SHAPE: tool_input ist nichtleere Liste"),
    # Ist-Verhalten, hier nur festgeschrieben: die LEERE Liste ist falsy
    # und wird von `data.get("tool_input") or {}` wie ein fehlendes tool_input
    # behandelt -> command == "" -> durchgelassen. Nichtleere Listen denyen
    # (Fall darueber).
    ('{"tool_input": []}', None, "nur SHAPE: tool_input ist LEERE Liste (Ist-Verhalten)"),
    ('{"tool_input": {"command": 123}}', "deny+unparseable", "= PAYLOAD command int"),
    ('{"tool_input": {"command": ["git", "push", "--force"]}}',
     "deny+unparseable", "= PAYLOAD command Liste"),
    # cwd-Guard auf DIESER Ebene: der TypeError, gegen den er gebaut
    # wurde, ist ein Prozess-Abbruch — in-process mit abgefangenem SystemExit
    # ist das nicht dasselbe Signal wie rc==0 plus deny-Ausgabe.
    ('{"tool_input": {"command": "git -C sub push"}, "cwd": 42}',
     "deny+cwd", "= PAYLOAD cwd int, hier end-to-end"),
    ('{"tool_input": {"command": "git push"}, "cwd": ["/tmp"]}',
     "deny+cwd", "= PAYLOAD cwd Liste, hier end-to-end"),
    # Regressionswaechter: legitimer "nichts zu pruefen"-Pfad. Kein Guard darf
    # diese Faelle in ein deny umdeuten.
    ('{}', None, "nur SHAPE: leeres dict, tool_input-Key fehlt ganz"),
    ('{"tool_input": null}', None, "= PAYLOAD tool_input null"),
    ('{"tool_input": {}}', None, "= PAYLOAD tool_input leer"),
    ('{"tool_input": {"command": ""}}', None, "= PAYLOAD leerer command-String"),
    ('{"tool_input": {"command": "git push"}}', None,
     "nur SHAPE: cwd-Key fehlt ganz (PAYLOAD hat nur cwd null/leer)"),
    ('{"tool_input": {"command": "git push"}, "cwd": null}', None,
     "= PAYLOAD cwd null bleibt nicht-aufloesbar"),
    ('{"tool_input": {"command": "git status"}, "cwd": "/tmp"}', None,
     "= PAYLOAD Nicht-Push-Kommando"),
    # Echte Treffer bleiben Treffer — kein Guard verschluckt sie.
    ('{"tool_input": {"command": "git push origin main"}, "cwd": "/tmp",'
     ' "permission_mode": "bypassPermissions"}', "deny", "= PAYLOAD Treffer bypass"),
    ('{"tool_input": {"command": "git push origin main"}, "cwd": "/tmp",'
     ' "permission_mode": "default"}', "ask", "= PAYLOAD Treffer default"),
    # Kaputtes JSON: bestehendes, bewusstes Verhalten (`except: sys.exit(0)`).
    # Hier NUR festgeschrieben, nicht bewertet und nicht geaendert.
    ('{not json', None, "nur SHAPE: brace-startendes Bruch-JSON (dokumentiert fail-open)"),
]


def _classify_shape_output(out):
    got = out.get("permissionDecision")
    if got != "deny":
        return got
    reason = out.get("permissionDecisionReason", "")
    if UNPARSEABLE_CWD_REASON in reason:
        return "deny+cwd"
    if UNPARSEABLE_REASON in reason:
        return "deny+unparseable"
    return got


def _run_shape_case(payload):
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
    return _classify_shape_output(out), proc.returncode


def _run_shape_cases():
    failures = 0
    for payload, expected, note in SHAPE_CASES:
        got, code = _run_shape_case(payload)
        ok = got == expected and code == 0
        failures += 0 if ok else 1
        detail = "" if ok else f"  (got={got!r} rc={code})"
        print(f"{'PASS' if ok else 'FAIL'} [{expected or 'pass '}] {note}: "
              f"{payload}{detail}")
    return failures


if __name__ == "__main__":
    main()
